#!/usr/bin/env python3
"""Target profiles: the operator-authored description of WHAT a run builds for (issue #284, R4-a).

A target profile is data under ``spec/targets/<target_id>.yaml``. It names one value per target
axis — the hardware class, the toolchain, the parallel model, the execution shape — and the
harness a node built for that target runs over. The operator writes it; no leaf chooses it. One
orchestration runs one node for one target (``tools/run_workflow.py --target``), and a closure's
members inherit the target of the run that drives them.

The values are OPAQUE TOKENS here, which is what lets this module live in the ``neutral core``
(``docs/BACKEND_BOUNDARY.md``): it never interprets a language, a build system or a parallel model,
it only asks the backend registry whether the host can serve the value. The profile document is
checked in this module's constants and the schema at ``spec/schema/targets/target_profile.schema.json``
is the declarative copy of the same facts (``spec/schema/SCHEMA.md``; the two are pinned together
by ``tools/tests/test_target_profile.py``).

Both layers are fail-closed. The loader (``load_target_profile``) refuses an unknown key, a
missing key, a wrongly typed value and a ``target_id`` that is not the file's stem; it does NOT
resolve the harness or ask the registry. The launch gate (``target_profile_violations``, run by
``resolve_run_target``) refuses a harness the catalog cannot resolve and a hardware class,
language, build system, parallel backend or pinned compiler the host does not implement; a
``hardware.architecture`` its class's ``perf_facts`` does not admit; a thread count, a rank count,
a process grid or an ``execution.sweep`` member the parallel backend does not run (issues #316,
#512); and, for a run that reaches ``Validate``, a hardware class this host cannot launch on or a
parallel backend it has no launch environment for (issue #289). ``toolchain.standard`` and ``toolchain.linker`` are recorded tokens
it does not check. A profile the host would have to guess about is a run nobody can reproduce.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.derivation import canonical_json_bytes, sha256_hex

TARGET_PROFILE_VERSION = 1
TARGETS_DIR = "spec/targets"
TARGET_PROFILE_SUFFIX = ".yaml"

#: `target_id` grammar; also the file stem. A plain identifier, so a target id can be a path
#: segment of the store without escaping it.
TARGET_ID_PATTERN = re.compile(r"[a-z][a-z0-9_]*")
#: The store-id grammar (`<slug>_<YYYYMMDD>_<seq3>`) every ir / pipeline / stage directory
#: takes — `orchestration_runtime._SLUG_DATE_SEQ3_PATTERN`, restated because this module must
#: not import the runtime at load time. A target id matching it is REFUSED: the level under
#: `workspace/pipelines/<safe>/` holds target ids, and until R4-a PR-2 the same level held
#: pipeline ids, so the two namespaces are kept disjoint by construction rather than by
#: whichever reader happens to look first.
STORE_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}")

#: Where the per-target pipeline trees live: `workspace/pipelines/<node_key_safe>/<target_id>/
#: <pipeline_id>/`. Compile output (`workspace/ir/<node_key_safe>/<ir_id>/`) is target-free.
PIPELINES_ROOT = "workspace/pipelines"

#: An axis value: an opaque token, lowercase so no reader has to normalize case.
TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.+-]*")

#: The phases a run can stop at WITHOUT executing the binary, compared case-insensitively. A
#: run ending at one of them is not asked the registry's execution half of the launch gate,
#: because building for a class launches no binary (issue #289, R4-b PR-1; whether a MACHINE of
#: the class is reachable is asked from `Build`, `NON_BUILDING_PHASES`); every other `until_phase`,
#: an unstated or misspelled one included, is — the set names what is EXEMPT, so a spelling
#: nobody listed lands on the refusing side.
NON_EXECUTING_PHASES = frozenset({"compile", "generate", "build"})
#: The phases a run can stop at WITHOUT BUILDING the binary, compared the same way: a subset of
#: `NON_EXECUTING_PHASES`. A binary is built at the execution site that runs it (issue #333), so
#: a run that reaches `Build` asks the site half of the launch gate — whether the site the target
#: maps to executes its class, and, for a remote site, the launch probe — and one ending at a
#: phase named here is asked neither. Named as what is EXEMPT, like the set above.
NON_BUILDING_PHASES = frozenset({"compile", "generate"})

#: The closed document shape: for each object, `(required keys, optional keys)`. The top level is
#: keyed by `""`. `tools/tests/test_target_profile.py` pins this table against the schema.
PROFILE_SHAPE: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "": (
        frozenset({"target_profile_version", "target_id", "hardware", "toolchain",
                   "parallel", "execution", "harness"}),
        frozenset(),
    ),
    "hardware": (frozenset({"class"}), frozenset({"architecture"})),
    "toolchain": (frozenset({"language", "standard", "build_system"}),
                  frozenset({"compiler", "linker"})),
    "parallel": (frozenset({"backend"}), frozenset()),
    "execution": (frozenset({"threads_per_rank"}),
                  frozenset({"ranks", "process_grid", "sweep"})),
    "harness": (frozenset({"infrastructure_id", "version_constraint"}), frozenset()),
}

#: The members `execution.sweep` may name (issue #512): each is a list of values the execution
#: shape takes in turn, one more run of the binary per value. `grids` varies the process grid
#: (a launcher backend's), `threads_per_rank` the threads of each rank.
SWEEP_KEYS = frozenset({"grids", "threads_per_rank"})

#: The label of the shape the certifying run takes — the profile's own `execution` — among the
#: shapes `TargetProfile.execution_shapes` lists.
CERTIFIED_SHAPE_LABEL = "certified"


@dataclass(frozen=True)
class ExecutionShape:
    """One shape a Validate run launches the binary in (issue #512): `threads_per_rank` threads
    in each of `ranks` processes, the ranks arranged in `process_grid` when the shape imposes
    one (None: the harness's own arrangement, `(ranks, 1)`). `label` names it in the records
    (`quality_check.json#shapes`), and is unique among a profile's shapes."""

    label: str
    threads_per_rank: int
    ranks: int
    process_grid: tuple[int, int] | None

    def record(self) -> dict[str, Any]:
        return {"label": self.label, "threads_per_rank": self.threads_per_rank,
                "ranks": self.ranks,
                "process_grid": None if self.process_grid is None else list(self.process_grid)}


class TargetProfileError(ValueError):
    """A target profile cannot be selected or loaded. `reason` is the stable refusal code the
    driver reports; the message says what is wrong and where."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class TargetProfile:
    """One loaded, shape-checked target profile. `sha256` is taken over the canonical JSON of
    the parsed document, so a comment or a key reordering in the YAML does not change it."""

    target_id: str
    doc: dict[str, Any]
    sha256: str

    @property
    def hardware_class(self) -> str:
        return str(self.doc["hardware"]["class"])

    @property
    def toolchain(self) -> dict[str, Any]:
        return dict(self.doc["toolchain"])

    @property
    def parallel_backend(self) -> str:
        return str(self.doc["parallel"]["backend"])

    @property
    def threads_per_rank(self) -> int:
        return int(self.doc["execution"]["threads_per_rank"])

    @property
    def ranks(self) -> int:
        """The processes a run starts (`execution.ranks`, issue #316); 1 when the profile states
        none, which is every profile written before it existed — so their documents, and the
        sha256 taken over them, are unchanged."""
        return int(self.doc["execution"].get("ranks", 1))

    @property
    def declares_process_grid(self) -> bool:
        """Whether the profile imposes a process grid on its run (`execution.process_grid`,
        issue #512); a profile that imposes none leaves its document, and its sha256, as they
        were."""
        return "process_grid" in self.doc["execution"]

    @property
    def process_grid(self) -> tuple[int, int]:
        """The `(p1, p2)` grid the run's ranks are arranged in: the profile's
        `execution.process_grid`, or `(ranks, 1)` — the arrangement a harness gives a run on
        which none is imposed — when it states none."""
        grid = self.doc["execution"].get("process_grid")
        if grid is None:
            return (self.ranks, 1)
        return (int(grid[0]), int(grid[1]))

    @property
    def certified_shape(self) -> ExecutionShape:
        """The shape the certifying run takes: the profile's own `execution`."""
        return ExecutionShape(CERTIFIED_SHAPE_LABEL, self.threads_per_rank, self.ranks,
                              self.process_grid if self.declares_process_grid else None)

    @property
    def sweep_shapes(self) -> list[ExecutionShape]:
        """The further shapes `execution.sweep` names (issue #512), in the order it names them:
        one per process grid (`grid_<p1>x<p2>`: `p1 p2` ranks in that grid, the certified
        threads per rank), then one per thread count (`threads_<t>`: the certified ranks and
        grid). Empty when the profile declares no sweep."""
        sweep = self.doc["execution"].get("sweep") or {}
        out = [ExecutionShape(f"grid_{p1}x{p2}", self.threads_per_rank, p1 * p2, (p1, p2))
               for p1, p2 in ((int(g[0]), int(g[1])) for g in sweep.get("grids") or ())]
        certified = self.certified_shape
        out += [ExecutionShape(f"threads_{int(t)}", int(t), certified.ranks,
                               certified.process_grid)
                for t in sweep.get("threads_per_rank") or ()]
        return out

    @property
    def execution_shapes(self) -> list[ExecutionShape]:
        """Every shape a Validate run of this target launches the binary in: the certified
        shape first, then the sweep's. Each is compared with the one-process, one-thread serial
        reference the quality check runs (`docs/workflow/phases/phase_04_validate.md` §4-2)."""
        return [self.certified_shape, *self.sweep_shapes]

    @property
    def harness(self) -> dict[str, str]:
        return dict(self.doc["harness"])

    def record(self, repo_root: Path) -> dict[str, Any]:
        """The provenance record an orchestration carries (`invocation.target`)."""
        return {
            "target_id": self.target_id,
            "sha256": self.sha256,
            "harness_node_key": harness_node_key_for_target(repo_root, self),
        }


def is_target_id(token: Any) -> bool:
    """Whether `token` is a well-formed target id: the id grammar, and not a store id."""
    return (isinstance(token, str) and bool(TARGET_ID_PATTERN.fullmatch(token))
            and not STORE_ID_PATTERN.fullmatch(token))


def pipelines_dir(node_key_safe: str, target_id: str) -> str:
    """`workspace/pipelines/<node_key_safe>/<target_id>` — the directory every pipeline of one
    node built for one target lives in. The ONE spelling of the store coordinate."""
    return f"{PIPELINES_ROOT}/{node_key_safe}/{target_id}"


def pipeline_ref_for(node_key_safe: str, target_id: str, pipeline_id: str) -> str:
    """The repo-relative ref of one pipeline: `<pipelines_dir>/<pipeline_id>`."""
    return f"{pipelines_dir(node_key_safe, target_id)}/{pipeline_id}"


def pipeline_target_id(ref: Any) -> str | None:
    """The target id a pipeline path (or any path beneath one) was built for, read off the
    store coordinate `workspace/pipelines/<safe>/<target_id>/<pipeline_id>/…`; None when `ref`
    is not under a per-target pipeline tree — a pre-R4-a pipeline directory
    (`workspace/pipelines/<safe>/<pipeline_id>/`) included, because its third segment is a
    store id and never a target id.

    This is how a reader that holds only a path learns the target: the host minted the path, so
    the coordinate is the host's own statement of what the artifacts under it were built for,
    never a field a leaf authored."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    parts = [p for p in ref.strip().replace("\\", "/").split("/") if p not in ("", ".")]
    head = PIPELINES_ROOT.split("/")
    for i in range(len(parts) - len(head) - 2):
        if parts[i:i + len(head)] == head:
            token = parts[i + len(head) + 1]
            return token if is_target_id(token) else None
    return None


def load_pipeline_target(repo_root: Path, ref: Any) -> TargetProfile:
    """The target profile a pipeline path was built for (`pipeline_target_id`), loaded.
    Refuses (`target_unresolved`) a path that carries no target coordinate, and
    (`target_profile_invalid`) a coordinate whose profile is gone or malformed — a reader that
    cannot tell what an artifact was built for must not guess."""
    target_id = pipeline_target_id(ref)
    if target_id is None:
        raise TargetProfileError(
            "target_unresolved",
            f"{ref!r} is not under a per-target pipeline tree "
            f"({PIPELINES_ROOT}/<node_key_safe>/<target_id>/<pipeline_id>)")
    return load_target_profile(repo_root, target_id)


def _targets_dir(repo_root: Path) -> Path:
    return Path(repo_root) / TARGETS_DIR


def list_target_ids(repo_root: Path) -> list[str]:
    """The target ids declared under `spec/targets/`, sorted. A `*.yaml` whose stem is not a
    valid target id is REFUSED rather than skipped, and so is a `*.yml`: a file the operator
    meant as a profile and misspelled must not make a different profile the only one, and so
    the default. Any other file is not a profile and is ignored."""
    root = _targets_dir(repo_root)
    if not root.is_dir():
        return []
    ids: list[str] = []
    for path in sorted(root.iterdir()):
        if path.name.endswith(".yml"):
            raise TargetProfileError(
                "target_profile_invalid",
                f"{TARGETS_DIR}/{path.name}: a target profile is named "
                f"<target_id>{TARGET_PROFILE_SUFFIX}; rename it")
        if not path.name.endswith(TARGET_PROFILE_SUFFIX):
            continue
        stem = path.name[: -len(TARGET_PROFILE_SUFFIX)]
        if not is_target_id(stem):
            raise TargetProfileError(
                "target_profile_invalid",
                f"{TARGETS_DIR}/{path.name}: the file stem {stem!r} is not a target id "
                f"(pattern {TARGET_ID_PATTERN.pattern}, and not a store id "
                f"{STORE_ID_PATTERN.pattern})")
        ids.append(stem)
    return ids


def select_target_id(repo_root: Path, requested: str | None) -> str:
    """The target a run builds for. An explicit `requested` must name a declared profile. With
    none requested, the ONLY declared profile is the default; with several, the operator must
    choose, because no default here could avoid naming a technology in the `neutral core`."""
    ids = list_target_ids(repo_root)
    if requested is not None:
        token = requested.strip()
        if token not in ids:
            raise TargetProfileError(
                "target_unknown",
                f"--target {requested!r} names no profile under {TARGETS_DIR}/ "
                f"(declared: {', '.join(ids) or 'none'})")
        return token
    if not ids:
        raise TargetProfileError(
            "no_target_profile",
            f"no target profile is declared under {TARGETS_DIR}/; add "
            f"{TARGETS_DIR}/<target_id>{TARGET_PROFILE_SUFFIX} "
            f"(schema: spec/schema/targets/target_profile.schema.json)")
    if len(ids) > 1:
        raise TargetProfileError(
            "target_required",
            f"{len(ids)} target profiles are declared ({', '.join(ids)}); choose one with "
            f"--target <id>")
    return ids[0]


def _shape_violations(doc: Any) -> list[str]:
    """Every way `doc` departs from `PROFILE_SHAPE` and the per-field grammar. Collected rather
    than stopping at the first, so one edit fixes a profile."""
    if not isinstance(doc, dict):
        return [f"the document must be a mapping, got {type(doc).__name__}"]
    out: list[str] = []
    for obj_key, (required, optional) in PROFILE_SHAPE.items():
        obj = doc if obj_key == "" else doc.get(obj_key)
        where = obj_key or "top level"
        if obj_key and not isinstance(obj, dict):
            if obj_key in doc:
                out.append(f"{where}: must be a mapping")
            continue
        assert isinstance(obj, dict)
        keys = set(obj)
        for missing in sorted(required - keys):
            out.append(f"{where}: missing required key {missing!r}")
        for unknown in sorted(keys - required - optional, key=str):
            out.append(f"{where}: unknown key {unknown!r}")
    version = doc.get("target_profile_version")
    if "target_profile_version" in doc and (
            isinstance(version, bool) or version != TARGET_PROFILE_VERSION):
        out.append(f"target_profile_version: must be {TARGET_PROFILE_VERSION}, got {version!r}")
    target_id = doc.get("target_id")
    if "target_id" in doc and not is_target_id(target_id):
        out.append(f"target_id: {target_id!r} does not match {TARGET_ID_PATTERN.pattern} "
                   f"(or is a store id)")

    def token(obj_key: str, key: str) -> None:
        obj = doc.get(obj_key)
        if not isinstance(obj, dict) or key not in obj:
            return
        value = obj[key]
        if not (isinstance(value, str) and TOKEN_PATTERN.fullmatch(value)):
            out.append(f"{obj_key}.{key}: {value!r} is not a lowercase token "
                       f"({TOKEN_PATTERN.pattern})")

    for obj_key, key in (("hardware", "class"), ("hardware", "architecture"),
                         ("toolchain", "language"), ("toolchain", "standard"),
                         ("toolchain", "build_system"), ("toolchain", "compiler"),
                         ("toolchain", "linker"), ("parallel", "backend"),
                         ("harness", "infrastructure_id")):
        token(obj_key, key)
    execution = doc.get("execution")
    if isinstance(execution, dict) and "threads_per_rank" in execution:
        threads = execution["threads_per_rank"]
        if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
            out.append(f"execution.threads_per_rank: must be an integer >= 1, got {threads!r}")
    if isinstance(execution, dict) and "ranks" in execution:
        ranks = execution["ranks"]
        if isinstance(ranks, bool) or not isinstance(ranks, int) or ranks < 1:
            out.append(f"execution.ranks: must be an integer >= 1, got {ranks!r}")
    if isinstance(execution, dict) and "process_grid" in execution:
        grid = execution["process_grid"]
        ranks = execution.get("ranks", 1)
        if not (isinstance(grid, list) and len(grid) == 2
                and all(type(p) is int and p >= 1 for p in grid)):
            out.append(f"execution.process_grid: must be a list of two integers >= 1, "
                       f"got {grid!r}")
        elif type(ranks) is int and grid[0] * grid[1] != ranks:
            out.append(f"execution.process_grid: {grid!r} arranges {grid[0] * grid[1]} "
                       f"rank(s), and execution.ranks is {ranks!r}")
    if isinstance(execution, dict) and "sweep" in execution:
        out += _sweep_violations(execution)
    harness = doc.get("harness")
    if isinstance(harness, dict) and "version_constraint" in harness:
        constraint = harness["version_constraint"]
        if not (isinstance(constraint, str) and constraint.strip()):
            out.append(f"harness.version_constraint: must be a non-empty string, "
                       f"got {constraint!r}")
    return out


def _sweep_violations(execution: dict[str, Any]) -> list[str]:
    """The grammar of `execution.sweep` (issue #512): a non-empty mapping of `SWEEP_KEYS`, each a
    non-empty list of distinct values. A grid is two integers >= 1 arranging at most
    `execution.ranks` ranks — the machine the profile is written for runs that many — other than
    `[1, 1]`, the serial reference every run already takes, and other than the certified run's
    own grid. A thread count is an integer >= 2 other than the certified `threads_per_rank`
    (one thread is the serial reference). Which backends admit each member is the launch gate's
    (`hardware_violations`)."""
    sweep = execution["sweep"]
    if not isinstance(sweep, dict) or not sweep:
        return [f"execution.sweep: must be a non-empty mapping of {sorted(SWEEP_KEYS)}, "
                f"got {sweep!r}"]
    out = [f"execution.sweep: unknown key {k!r}" for k in sorted(set(sweep) - SWEEP_KEYS, key=str)]
    ranks = execution.get("ranks", 1)
    grid = execution.get("process_grid")
    certified_grid = grid if isinstance(grid, list) else [ranks, 1]
    if "grids" in sweep:
        grids = sweep["grids"]
        if not (isinstance(grids, list) and grids and all(
                isinstance(g, list) and len(g) == 2 and all(type(p) is int and p >= 1 for p in g)
                for g in grids)):
            out.append(f"execution.sweep.grids: must be a non-empty list of [p1, p2] pairs of "
                       f"integers >= 1, got {grids!r}")
        else:
            seen: list[list[int]] = []
            for g in grids:
                if g in seen:
                    out.append(f"execution.sweep.grids: {g!r} is listed twice")
                seen.append(g)
                if g == [1, 1]:
                    out.append("execution.sweep.grids: [1, 1] is the serial reference every "
                               "run already takes")
                elif g == certified_grid:
                    out.append(f"execution.sweep.grids: {g!r} is the certified run's own grid")
                elif type(ranks) is int and g[0] * g[1] > ranks:
                    out.append(f"execution.sweep.grids: {g!r} arranges {g[0] * g[1]} ranks, "
                               f"more than execution.ranks ({ranks!r})")
    if "threads_per_rank" in sweep:
        counts = sweep["threads_per_rank"]
        if not (isinstance(counts, list) and counts
                and all(type(t) is int and t >= 2 for t in counts)):
            out.append(f"execution.sweep.threads_per_rank: must be a non-empty list of "
                       f"integers >= 2 (one thread is the serial reference), got {counts!r}")
        else:
            for i, t in enumerate(counts):
                if t in counts[:i]:
                    out.append(f"execution.sweep.threads_per_rank: {t} is listed twice")
                elif t == execution.get("threads_per_rank"):
                    out.append(f"execution.sweep.threads_per_rank: {t} is the certified run's "
                               f"own threads_per_rank")
    return out


def load_target_profile(repo_root: Path, target_id: str) -> TargetProfile:
    """Read and shape-check `spec/targets/<target_id>.yaml`. Refuses (`target_profile_invalid`)
    a missing or unparseable file, any shape violation, and a `target_id` that is not the stem."""
    import yaml

    if not is_target_id(target_id):
        raise TargetProfileError(
            "target_profile_invalid",
            f"{target_id!r} is not a target id (pattern {TARGET_ID_PATTERN.pattern}, and "
            f"not a store id {STORE_ID_PATTERN.pattern})")
    rel = f"{TARGETS_DIR}/{target_id}{TARGET_PROFILE_SUFFIX}"
    try:
        text = (Path(repo_root) / rel).read_text(encoding="utf-8")
        doc = yaml.safe_load(text)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise TargetProfileError("target_profile_invalid", f"{rel}: unreadable: {exc}") from exc
    violations = _shape_violations(doc)
    if not violations and doc["target_id"] != target_id:
        violations.append(
            f"target_id: {doc['target_id']!r} is not the file stem {target_id!r}")
    if violations:
        raise TargetProfileError("target_profile_invalid", f"{rel}: {'; '.join(violations)}")
    _fill_default_architecture(doc)
    return TargetProfile(target_id=target_id, doc=doc, sha256=sha256_hex(canonical_json_bytes(doc)))


def _fill_default_architecture(doc: dict[str, Any]) -> None:
    """Give a profile that states no `hardware.architecture` its class's default, when the
    class's `perf_facts` declare one (`DEFAULT_ARCHITECTURE`; the GPU class's is `all`, device
    code for every architecture the compiler supports). Filled BEFORE the profile's `sha256` is
    taken, so the default is part of the target's identity: a change of it moves the Generate,
    Build and Validate keys of that target alone, as editing the file would. A class this
    repository does not implement is left as it is; `hardware_violations` refuses it at launch."""
    from tools.backends import registry as backend_registry

    hardware = doc["hardware"]
    hardware_class = hardware["class"]
    if "architecture" in hardware or \
            backend_registry.unimplemented_reason("hardware", hardware_class) is not None or \
            "perf_facts" not in backend_registry.get("hardware", hardware_class).backend_provides:
        return
    default = getattr(backend_registry.capability_module(
        "hardware", hardware_class, "perf_facts"), "DEFAULT_ARCHITECTURE", None)
    if default is not None:
        hardware["architecture"] = default


def harness_node_key_for_target(repo_root: Path, profile: TargetProfile) -> str:
    """The node_key of the harness `profile` names: its `infrastructure_id` at the highest
    catalog version satisfying its `version_constraint` — the version choice
    `tools/dependency_graph.py` makes for a dependency edge. Refuses (`target_profile_invalid`)
    a harness the catalog does not carry as an `infrastructure` spec, or whose constraint
    matches no version. The message does not name the target: every caller that reports it
    (`resolve_run_target`) prefixes the target itself, and naming it here printed it twice."""
    from tools.orchestration_runtime import (
        SpecCatalogCorruption,
        _load_spec_catalog,
        _matching_dep_versions,
    )

    harness = profile.harness
    infra_id = harness["infrastructure_id"]
    try:
        catalog = _load_spec_catalog(str(Path(repo_root).resolve()))
        matched = _matching_dep_versions(
            catalog, "infrastructure", infra_id, harness["version_constraint"])
    except SpecCatalogCorruption as exc:
        raise TargetProfileError(
            "target_profile_invalid",
            f"the harness cannot be resolved: {exc}") from exc
    if not matched:
        raise TargetProfileError(
            "target_profile_invalid",
            f"harness infrastructure/{infra_id} "
            f"{harness['version_constraint']!r} matches no `infrastructure` version in "
            f"spec/registry/spec_catalog.yaml")
    return f"infrastructure/{infra_id}@{matched[0]}"


def target_harness_entries(target: TargetProfile | None,
                           node_kind: Any) -> list[tuple[str, str, str]]:
    """The dependency entry a node gains from the target it is built for: the target's harness,
    as the `(spec_kind, spec_id, version_constraint)` triple `orchestration_runtime
    ._parse_dep_entries` yields for a `deps.yaml` entry — for every node whose kind is not
    `infrastructure`; none without a target (issue #284, R4-a PR-3).

    This is where the harness enters every closure and readiness question that has a target:
    `--with-deps`' closure (`run_workflow._resolve_dependency_closure`), the launch gate's direct
    set (`orchestration_runtime._certify_and_collect_dep_artifacts`) and its stale-dependency
    report. `deps.yaml` declared it until PR-3 (the spec-input gate now refuses a declaration),
    so a node's readiness asks for the harness at the same stages it asked before. What does NOT
    see it is the target-free Compile: the dependency graph and its sidecar, and the compile key,
    are `deps.yaml`'s alone; the pipeline closure adds it back
    (`orchestration_runtime.pipeline_closure_nodes`).

    `node_kind` is compared with `.strip()` and nothing else, the spelling rule every reader of
    `spec_kind` uses."""
    if target is None:
        return []
    if isinstance(node_kind, str) and node_kind.strip() == "infrastructure":
        return []
    harness = target.harness
    return [("infrastructure", harness["infrastructure_id"], harness["version_constraint"])]


#: What `Generate` asks of the target LANGUAGE for every node kind: `bundle_facts` (the bundle
#: contract's file facts and the host-given names), `syntax_promotions` (the syntax stage),
#: `prompt_fragments` (the generate prompts' rules and the runner-output binding) and
#: `checks_abi` (the checks-module binding and the gate guards), `source_reading` (the gates'
#: reader of a generated source), `signatures` (the `controlled_spec` §5.1 signature gates and the
#: dependency-interface extraction),
#: and `control_file` (the language
#: half of the build control file the host authors for every node: a `harness` bundle has no shape
#: without it — R4-b PR-4's round 3 moved it here from the non-`infrastructure` list, where a
#: language declaring it for no node passed launch and stopped mid-`Generate`). A capability the
#: phase reads for one kind only belongs in `toolchain_servable_reasons`' kind-specific list
#: instead.
LANGUAGE_CAPABILITIES_EVERY_NODE: tuple[str, ...] = (
    "bundle_facts", "syntax_promotions", "prompt_fragments", "checks_abi", "source_reading",
    "signatures", "control_file")


def toolchain_servable_reasons(language: str, build_system: str, *,
                               infrastructure: bool) -> list[str]:
    """Why the host cannot build and render a node of this kind in (`language`, `build_system`);
    empty when it can. ONE statement of the capability question, asked of a target profile at
    launch (`target_profile_violations`). Until R4-a PR-3 it was also asked of the admissible
    set a compile producer was shown, and of the IR's `impl_defaults.toolchain` at Compile;
    the target is the profile's alone now, so the launch gate is where it is asked.

    Every node needs its build system to be executable and its language to declare what the
    `Generate` phase asks of it (issue #289, R4-b PR-2): the bundle facts, the syntax-stage
    facts, the prompt fragments and the checks-ABI binding, and since R4-b PR-3 the source
    reader and the signature module the deterministic gates read every node's sources and §5.1
    surface with, and since R4-b PR-4 the control file's language half (both
    `LANGUAGE_CAPABILITIES_EVERY_NODE`); the build system's half of the control file too.
    A language missing one is refused HERE, at launch, rather than at the first dispatch that
    needs it — mid-run, after Compile has been billed — and never silently served with another
    language's rules. Every other kind than `infrastructure` also needs the host to render the
    runner (language)."""
    from tools.backends import registry as backend_registry

    reasons: list[str] = []
    for axis, value in (("language", language), ("build_system", build_system)):
        reason = backend_registry.unimplemented_reason(axis, value)
        if reason is not None:
            reasons.append(reason)
    if reasons:
        return reasons
    required: list[tuple[str, str, str]] = [("build_system", build_system, "build_execute"),
                                            ("build_system", build_system, "control_file")]
    required += [("language", language, c) for c in LANGUAGE_CAPABILITIES_EVERY_NODE]
    if not infrastructure:
        required += [("language", language, "runner_render")]
    for axis, value, capability in required:
        reason = backend_registry.missing_capability_reason(axis, value, capability)
        if reason is not None:
            reasons.append(reason)
    return reasons


def hardware_violations(profile: TargetProfile, *, until_phase: str | None = None) -> list[str]:
    """The `hardware` half of the launch gate (issue #289, R4-b PR-1).

    The class must be one this repository implements, and its `architecture`, when the profile
    states one, must satisfy the class's `perf_facts` when the class states them (a class that
    states none leaves it a recorded token, as every class did before the `hardware` axis
    existed). An absent `architecture` was given the class's `DEFAULT_ARCHITECTURE` when the
    profile was loaded (`_fill_default_architecture`); the operator pins one only when the build
    must target a particular device.

    `execution.ranks` above 1 needs a parallel backend that declares `launcher` (issue #316):
    without one the binary runs as one process whatever the profile says, and the run would be
    recorded as the rank count nothing ran with. Asked of every run, since the count is part of
    what the target is. `execution.process_grid` needs one too (issue #512): it arranges the
    ranks the launcher starts; so does `execution.sweep.grids`. Every thread count the profile
    runs a rank with — `threads_per_rank` and `execution.sweep.threads_per_rank` — must be one
    the backend's launch environment admits (`_thread_count_violations`).

    The EXECUTION half is asked of every run except one whose `until_phase` is in
    `NON_EXECUTING_PHASES` — None, the stricter question, is asked it. It
    requires the class to declare `execution` and the parallel backend to declare
    `execution_env`: the two questions `tools/host_execution.launch_shape` asks when
    `Validate.execute` launches the binary, asked here first so a run that would be refused
    there is refused before anything is billed. A run that stops earlier is not asked, because
    building for a class launches no binary. That admits the run, not its
    dependencies: a closure member is driven to Validate (`run_workflow`'s `dep_until_phase`) and
    asked there, and a node whose dependencies are not certified through Validate stops at the
    dependency-readiness gate. Every implemented class declares `execution` since issue #293, so
    whether a MACHINE of the class is reachable is the other half of the question, asked by the
    driver from an earlier phase — every run that reaches `Build`, since the binary is built at
    the site that runs it (issue #333): `execution_sites.site_violations`."""
    from tools.backends import registry as backend_registry

    out: list[str] = []
    hardware_class = profile.hardware_class
    reason = backend_registry.unimplemented_reason("hardware", hardware_class)
    if reason is not None:
        return [f"hardware.class: {reason}"]
    record = backend_registry.get("hardware", hardware_class)
    if "perf_facts" in record.backend_provides:
        facts = backend_registry.capability_module("hardware", hardware_class, "perf_facts")
        architecture = profile.doc["hardware"].get("architecture")
        if architecture is not None and \
                not facts.ARCHITECTURE_PATTERN.fullmatch(str(architecture)):
            out.append(f"hardware.architecture: {architecture!r} is not a {hardware_class} "
                       f"architecture (pattern {facts.ARCHITECTURE_PATTERN.pattern})")
    if profile.ranks != 1 and backend_registry.unimplemented_reason(
            "parallel", profile.parallel_backend) is None and not backend_registry.provides(
            "parallel", profile.parallel_backend, "launcher"):
        out.append(f"execution.ranks: {profile.ranks} ranks need a launcher, and parallel "
                   f"backend {profile.parallel_backend} declares none")
    if profile.declares_process_grid and backend_registry.unimplemented_reason(
            "parallel", profile.parallel_backend) is None and not backend_registry.provides(
            "parallel", profile.parallel_backend, "launcher"):
        out.append(f"execution.process_grid: a process grid arranges the ranks a launcher "
                   f"starts, and parallel backend {profile.parallel_backend} declares none")
    sweep = profile.doc["execution"].get("sweep") or {}
    if "grids" in sweep and backend_registry.unimplemented_reason(
            "parallel", profile.parallel_backend) is None and not backend_registry.provides(
            "parallel", profile.parallel_backend, "launcher"):
        out.append(f"execution.sweep.grids: a process grid arranges the ranks a launcher "
                   f"starts, and parallel backend {profile.parallel_backend} declares none")
    out += _thread_count_violations(profile)
    if str(until_phase or "").strip().lower() not in NON_EXECUTING_PHASES:
        for axis, value, capability, where in (
                ("hardware", hardware_class, "execution", "hardware.class"),
                ("parallel", profile.parallel_backend, "execution_env", "parallel.backend")):
            reason = backend_registry.missing_capability_reason(axis, value, capability)
            if reason is not None:
                out.append(f"{where}: a run that reaches Validate launches the binary, and "
                           f"{reason}")
    return out


def _thread_count_violations(profile: TargetProfile) -> list[str]:
    """Every thread count the profile runs a rank with other than one — `threads_per_rank` and
    each of `execution.sweep.threads_per_rank` (issue #512) — must be one the parallel
    backend's launch environment admits: its `execution_env` answers the count, or refuses it
    (`ValueError`, e.g. a model whose ranks run one thread each). A backend whose launch
    environment is the neutral core's empty one runs one thread whatever the profile says, so it
    admits none; a backend this repository does not implement is refused in its own words
    elsewhere."""
    from tools.backends import registry as backend_registry

    counts = [("execution.threads_per_rank", profile.threads_per_rank)]
    counts += [("execution.sweep.threads_per_rank", int(t))
               for t in (profile.doc["execution"].get("sweep") or {}).get(
                   "threads_per_rank") or ()]
    backend = profile.parallel_backend
    if backend_registry.missing_capability_reason("parallel", backend, "execution_env") \
            is not None:
        return []
    out: list[str] = []
    for where, count in counts:
        if count == 1:
            continue
        if "execution_env" not in backend_registry.get("parallel", backend).backend_provides:
            out.append(f"{where}: {count} threads per rank, and parallel backend {backend} "
                       f"launches each rank as one thread")
            continue
        module = backend_registry.capability_module("parallel", backend, "execution_env")
        try:
            module.environment(count)
        except ValueError as exc:
            out.append(f"{where}: parallel backend {backend} does not run {count} threads per "
                       f"rank ({exc})")
    return out


def target_profile_violations(repo_root: Path, profile: TargetProfile, *,
                              node_key: str | None = None,
                              until_phase: str | None = None) -> list[str]:
    """The launch gate over a loaded profile: every axis value is one this repository
    implements, the toolchain is servable for a node of `node_key`'s kind (a non-infrastructure
    node when `node_key` is None — the stricter question), the hardware half holds for a run
    ending at `until_phase` (`hardware_violations`; None is the stricter question again), and
    the harness resolves. An `infrastructure` node run for a target must BE that target's
    harness."""
    from tools.backends import registry as backend_registry

    out: list[str] = hardware_violations(profile, until_phase=until_phase)
    tc = profile.toolchain
    infrastructure = bool(node_key) and node_key.split("/", 1)[0] == "infrastructure"
    out += [f"toolchain: {r}" for r in toolchain_servable_reasons(
        tc["language"], tc["build_system"], infrastructure=infrastructure)]
    reason = backend_registry.unimplemented_reason("parallel", profile.parallel_backend)
    if reason is not None:
        out.append(f"parallel.backend: {reason}")
    if tc.get("compiler"):
        reason = backend_registry.unimplemented_reason("compiler", tc["compiler"])
        if reason is not None:
            out.append(f"toolchain.compiler: {reason}")
        # The build and the syntax stage run the backend's compiler wrapper in the compiler's
        # place (issue #316), so a pin would name a compiler nothing runs.
        if backend_registry.provides("parallel", profile.parallel_backend, "compiler_wrapper"):
            out.append(f"toolchain.compiler: {tc['compiler']} is pinned, and parallel backend "
                       f"{profile.parallel_backend} compiles through its compiler wrapper; "
                       f"remove the pin")
    # A compiler wrapper runs ONE compiler (`WRAPPED_COMPILER`), and the build pins the control
    # file's compiler variable to it whatever the language: a target whose language compiles
    # with another would build its sources with the wrapper's compiler (issue #316).
    if backend_registry.unimplemented_reason("parallel", profile.parallel_backend) is None and \
            "compiler_wrapper" in backend_registry.get(
                "parallel", profile.parallel_backend).backend_provides and \
            backend_registry.provides("language", tc["language"], "bundle_facts"):
        wrapped = str(backend_registry.capability_module(
            "parallel", profile.parallel_backend, "compiler_wrapper").WRAPPED_COMPILER)
        compiler = str(backend_registry.capability_module(
            "language", tc["language"], "bundle_facts").DEFAULT_COMPILER)
        if wrapped != compiler:
            out.append(f"parallel.backend: {profile.parallel_backend}'s compiler wrapper runs "
                       f"{wrapped}, and toolchain.language {tc['language']} compiles with "
                       f"{compiler}")
    try:
        harness_nk = harness_node_key_for_target(repo_root, profile)
    except TargetProfileError as exc:
        out.append(exc.detail)
        return out
    if infrastructure and harness_nk != node_key:
        out.append(
            f"target_harness_mismatch: {node_key} is an infrastructure node, and target "
            f"{profile.target_id} runs over {harness_nk}; run a harness only for the target "
            f"whose harness it is")
    out += _process_model_violations(profile, harness_nk)
    return out


def _process_model_violations(profile: TargetProfile, harness_nk: str) -> list[str]:
    """The parallel backend and the harness must agree on how many processes a program is
    (issue #316): a backend that declares a `launcher` starts several, and only a harness that
    provides `distributed_state` drives a program as several — the one that starts and ends the
    runtime and gathers the state onto one rank; a distributed harness, conversely, needs the
    launcher that starts its ranks. Either mismatch would certify nothing it claims: the runs of
    a single-process harness under a launcher write every output once per process, and a
    distributed harness run without one is a one-process program."""
    from tools.backends import registry as backend_registry
    from tools.codegen_bundle import capability_name, harness_provided_capabilities
    if backend_registry.unimplemented_reason("parallel", profile.parallel_backend) is not None:
        return []  # refused above, in the parallel axis' own words
    launches = backend_registry.provides("parallel", profile.parallel_backend, "launcher")
    distributed = any(capability_name(t) == "distributed_state"
                      for t in (harness_provided_capabilities(harness_nk) or ()))
    if launches and not distributed:
        return [(f"parallel.backend: {profile.parallel_backend} starts the program as several "
                 f"processes under its launcher, and harness {harness_nk} provides no "
                 f"distributed_state capability to drive them; name a harness that does")]
    if distributed and not launches:
        return [(f"harness: {harness_nk} drives the program as several processes "
                 f"(distributed_state), and parallel backend {profile.parallel_backend} declares "
                 f"no launcher to start them; use a parallel backend that does")]
    return []


def resolve_run_target(repo_root: Path, requested: str | None, *,
                       node_key: str | None = None,
                       until_phase: str | None = None) -> TargetProfile:
    """Select, load and gate the target of one run ending at `until_phase`. Raises
    `TargetProfileError`."""
    target_id = select_target_id(repo_root, requested)
    profile = load_target_profile(repo_root, target_id)
    violations = target_profile_violations(repo_root, profile, node_key=node_key,
                                           until_phase=until_phase)
    if violations:
        reason = ("target_harness_mismatch"
                  if violations[0].startswith("target_harness_mismatch")
                  else "target_profile_invalid")
        raise TargetProfileError(reason, f"target {target_id}: {violations[0]}")
    return profile
