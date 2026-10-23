"""The work tree of a `workshop leaf` lineage: its paths, its seeds, its record, its snapshot.

`docs/design/producer_workshop_leaf.md` decides the shape (issue #534): a PRODUCING leaf works
in a host-seeded tree, and the host copies the tree's final state into the existing
`CodegenBundle` contract after the leaf exits. This module is the host's half of that copy and
of the lineage's bookkeeping. It holds no leaf launch and no gate: the document it assembles goes
through the unchanged `codegen_bundle.pure_bundle_contract_violation`, and every artifact a later
`phase` reads is still written by the conductor from that document.

Two places per lineage, and the leaf sees only the first:

* the LINEAGE HOME, `<homes-root>/<oid>/<backend>/<lineage_id>/` (outside the checkout, like
  the codex lineage home of issue #245): `config/` (the CLI's private config home), `tree/` (the
  work tree), `staged/obj` + `staged/bin` (the wrapper's object and binary directories, which
  hold the staged dependency closure), `syntax/` (syntax-check stage directories) and
  `snapshot/` (the tree's non-host-placed files, copied when the lineage ends);
* the LINEAGE RECORD, `workspace/orchestrations/<oid>/workshop/<lineage_id>/`, host-written
  only: `workshop_lineage.json`, `command_log.jsonl` (every build-runtime call of the wrapper) and
  `wrapper_calls.jsonl` (every request the wrapper served).

No function here raises on what the leaf wrote: a malformed tree is a finding the caller
reports back, never an exception. Neutral core (`docs/BACKEND_BOUNDARY.md`): the names of the
seeds and of the control file are the caller's.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import shutil
import stat
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

#: The manifest the leaf writes at the tree root: the bundle document minus `files[].content`.
BUNDLE_MANIFEST_NAME = "bundle.yaml"

#: The category of a tree that holds no manifest (`docs/design/producer_workshop_leaf.md`
#: §Output files in the tree).
WORKSHOP_TREE_EMPTY = "workshop_tree_empty"
#: The category of every other assembly failure — the pure producer's own malformed-bundle
#: category, so the routing of a defective document does not depend on its transport.
BUNDLE_SCHEMA_VIOLATION = "bundle_schema_violation"

#: Why a lineage ended. `None` while it is open.
ENDED_BY_VALUES: tuple[str, ...] = (
    "leaf_exit", "max_wrapper_calls", "lineage_timeout", "leaf_timeout")

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_LINEAGE_LOCKS: dict[str, threading.Lock] = {}
_LINEAGE_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class LineagePaths:
    """Every path of one lineage. `homes_root` is `operator_private_root.workflow_homes_root()`
    in production; `orchestration_dir` is `workspace/orchestrations/<oid>`."""

    homes_root: Path
    orchestration_id: str
    lineage_id: str
    orchestration_dir: Path
    backend: str = "claude"

    def __post_init__(self) -> None:
        for name in ("orchestration_id", "lineage_id", "backend"):
            value = getattr(self, name)
            if not (isinstance(value, str) and _SAFE_ID_RE.fullmatch(value)):
                raise ValueError(
                    f"lineage {name} must be a plain [A-Za-z0-9_-] token (got {value!r})")

    @property
    def home(self) -> Path:
        return Path(self.homes_root) / self.orchestration_id / self.backend / self.lineage_id

    @property
    def config(self) -> Path:
        return self.home / "config"

    @property
    def tree(self) -> Path:
        return self.home / "tree"

    @property
    def staged(self) -> Path:
        return self.home / "staged"

    @property
    def staged_obj(self) -> Path:
        return self.staged / "obj"

    @property
    def staged_bin(self) -> Path:
        return self.staged / "bin"

    @property
    def syntax(self) -> Path:
        return self.home / "syntax"

    @property
    def snapshot(self) -> Path:
        return self.home / "snapshot"

    @property
    def record_dir(self) -> Path:
        return Path(self.orchestration_dir) / "workshop" / self.lineage_id

    @property
    def record_json(self) -> Path:
        return self.record_dir / "workshop_lineage.json"

    @property
    def command_log(self) -> Path:
        return self.record_dir / "command_log.jsonl"

    @property
    def wrapper_calls(self) -> Path:
        return self.record_dir / "wrapper_calls.jsonl"

    def make_work_dirs(self) -> None:
        """Create the home's work directories (all but `config/`, which the CLI home's own
        preparer owns) and the record directory. The home itself must already exist: it is
        created exclusively, 0700, by that preparer, and never here."""
        if not self.home.is_dir():
            raise ValueError(f"lineage home {self.home} does not exist")
        for directory in (self.tree, self.staged_obj, self.staged_bin, self.syntax,
                          self.snapshot):
            directory.mkdir(parents=True, exist_ok=True)
        self.record_dir.mkdir(parents=True, exist_ok=True)


def reserved_tree_names(seed_names: Iterable[str], control_file_basename: str) -> frozenset[str]:
    """The tree paths a bundle may not name as one of its files: every seed, the derived
    control file and the manifest. A bundle naming one is refused at `derive` and at the
    snapshot alike, because the host — not the leaf — writes what these names hold."""
    names = {str(n) for n in seed_names}
    names.add(str(control_file_basename))
    names.add(BUNDLE_MANIFEST_NAME)
    return frozenset(names)


def plain_relative_path(name: str) -> PurePosixPath | None:
    """`name` as a relative POSIX path with no empty, `.` or `..` segment, else None."""
    if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name:
        return None
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return PurePosixPath(name)


def seed_tree(paths: LineagePaths, seeds: Mapping[str, str | bytes]) -> tuple[str, ...]:
    """Write each seed at `<tree>/<name>` and return the names, sorted. Refuses a name that is
    not a plain relative path, and a tree that already holds a file (a seed lands in a fresh
    tree only: a cold reuse copies the prior snapshot in AFTER seeding)."""
    tree = paths.tree
    tree.mkdir(parents=True, exist_ok=True)
    if any(tree.iterdir()):
        raise ValueError(f"work tree {tree} is not empty; seeds land in a fresh tree")
    for name, content in seeds.items():
        rel = plain_relative_path(name)
        if rel is None:
            raise ValueError(f"seed name {name!r} is not a plain relative path")
        target = tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        target.write_bytes(data)
    return tuple(sorted(seeds))


# --- the lineage record ---------------------------------------------------------------


def _lineage_lock(paths: LineagePaths) -> threading.Lock:
    key = str(paths.record_json)
    with _LINEAGE_LOCKS_GUARD:
        return _LINEAGE_LOCKS.setdefault(key, threading.Lock())


def _write_json_atomic(path: Path, doc: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class _Locked:
    """The lineage record's lock: a thread lock (handler threads of one process) and a `flock`
    on a lock file beside the record (a second process of the same run)."""

    def __init__(self, paths: LineagePaths) -> None:
        self._thread_lock = _lineage_lock(paths)
        self._lock_path = paths.record_dir / ".lineage.lock"
        self._fd: int | None = None

    def __enter__(self) -> None:
        self._thread_lock.acquire()
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except BaseException:
            self._thread_lock.release()
            raise

    def __exit__(self, *exc: object) -> None:
        try:
            if self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
        finally:
            self._thread_lock.release()


def write_lineage_record(paths: LineagePaths, *, node_key: str,
                       started_at_utc: str) -> dict[str, Any]:
    """Create `workshop_lineage.json` for a new lineage, exclusively: a lineage starts once."""
    doc = {
        "lineage_id": paths.lineage_id,
        "node_key": node_key,
        "started_at_utc": started_at_utc,
        "wrapper_calls_used": 0,
        "ended_by": None,
    }
    with _Locked(paths):
        if paths.record_json.exists():
            raise ValueError(f"lineage record already exists: {paths.record_json}")
        _write_json_atomic(paths.record_json, doc)
    return doc


def read_lineage_record(paths: LineagePaths) -> dict[str, Any]:
    """The lineage record. Raises `ValueError` when it is absent or not an object."""
    try:
        doc = json.loads(paths.record_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"lineage record unreadable: {paths.record_json}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"lineage record is not an object: {paths.record_json}")
    return doc


def end_lineage(paths: LineagePaths, ended_by: str) -> str:
    """Record why the lineage ended, unless it already ended, and return the recorded reason.
    The FIRST reason stands: a bound that ended the lineage is not overwritten by the leaf's
    exit that the bound's kill caused."""
    if ended_by not in ENDED_BY_VALUES:
        raise ValueError(f"ended_by must be one of {ENDED_BY_VALUES} (got {ended_by!r})")
    with _Locked(paths):
        doc = read_lineage_record(paths)
        if doc.get("ended_by") is None:
            doc["ended_by"] = ended_by
            _write_json_atomic(paths.record_json, doc)
        return str(doc["ended_by"])


def count_wrapper_call(paths: LineagePaths, cap: int) -> bool:
    """Count one wrapper call against the lineage's `max_wrapper_calls` and return whether it
    may run. The count is the lineage's, persisted in `workshop_lineage.json`, so it carries across every
    launch of the lineage. The call that would exceed `cap` is refused and ends the lineage
    (`ended_by = max_wrapper_calls`); so is any call on a lineage that has already ended."""
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise ValueError(f"max_wrapper_calls must be an integer >= 1 (got {cap!r})")
    with _Locked(paths):
        doc = read_lineage_record(paths)
        if doc.get("ended_by") is not None:
            return False
        used = int(doc.get("wrapper_calls_used") or 0)
        if used >= cap:
            doc["ended_by"] = "max_wrapper_calls"
            _write_json_atomic(paths.record_json, doc)
            return False
        doc["wrapper_calls_used"] = used + 1
        _write_json_atomic(paths.record_json, doc)
        return True


def append_jsonl(path: Path, entry: Mapping[str, Any]) -> None:
    """Append one JSON line to a lineage record file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --- assembly -------------------------------------------------------------------------


@dataclass(frozen=True)
class AssemblyFailure:
    """A tree whose manifest could not become a bundle document: the category the producer's
    failure routing reads, and the findings the repair turn carries."""

    category: str
    findings: str


def regular_file_under(root: Path, rel: PurePosixPath) -> tuple[Path | None, str | None]:
    """`(path, None)` when `<root>/<rel>` is a regular file reached through no symbolic link,
    else `(None, why)`. Every component is `lstat`ed: a link anywhere on the way is refused,
    whether it points in or out of the tree (a row-2 binary can create one in the rw tree)."""
    current = root
    for index, part in enumerate(rel.parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None, "does not exist"
        except OSError as exc:
            return None, f"cannot be read ({exc.strerror or exc})"
        if stat.S_ISLNK(info.st_mode):
            return None, "is reached through a symbolic link"
        last = index == len(rel.parts) - 1
        if last and not stat.S_ISREG(info.st_mode):
            return None, "is not a regular file"
        if not last and not stat.S_ISDIR(info.st_mode):
            return None, "is not reachable (a component is not a directory)"
    return current, None


def assemble_bundle_document(root: Path, reserved: Iterable[str]) -> dict[str, Any] | AssemblyFailure:
    """The bundle document `root`'s manifest describes, with every `files[].content` read from
    the file its `logical_path` names under `root` — or the `AssemblyFailure` that stops it.

    `root` is the work tree (for the wrapper's `derive`) or the lineage's snapshot (for the
    hand-off). The manifest is `yaml.safe_load`ed and must be a mapping whose values are JSON
    values; a `files[]` entry carrying `content` itself is refused (the tree, not the manifest,
    holds the text). A `logical_path` that is absolute, holds a `..` or empty segment, names a
    reserved tree path, is reached through a symbolic link, or is missing, a directory, or not
    UTF-8 is a finding. Every finding of the document is reported at once. Whatever passes here
    still goes through `codegen_bundle.pure_bundle_contract_violation`, which owns the grammar of
    a `logical_path` and every other rule of the document; this function decides only what can
    be READ. Never raises on the tree's content."""
    root = Path(root)
    reserved_set = frozenset(reserved)
    manifest, why = regular_file_under(root, PurePosixPath(BUNDLE_MANIFEST_NAME))
    if manifest is None:
        if why == "does not exist":
            return AssemblyFailure(
                WORKSHOP_TREE_EMPTY,
                f"the tree holds no {BUNDLE_MANIFEST_NAME}: write the bundle manifest at the "
                "tree root, naming every bundle file")
        return AssemblyFailure(BUNDLE_SCHEMA_VIOLATION, f"{BUNDLE_MANIFEST_NAME} {why}")
    try:
        text = manifest.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return AssemblyFailure(BUNDLE_SCHEMA_VIOLATION, f"{BUNDLE_MANIFEST_NAME} is not UTF-8")
    except OSError as exc:
        return AssemblyFailure(BUNDLE_SCHEMA_VIOLATION,
                               f"{BUNDLE_MANIFEST_NAME} cannot be read: {exc}")
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return AssemblyFailure(BUNDLE_SCHEMA_VIOLATION,
                               f"{BUNDLE_MANIFEST_NAME} is not valid YAML: {exc}")
    if not isinstance(loaded, dict):
        return AssemblyFailure(
            BUNDLE_SCHEMA_VIOLATION,
            f"{BUNDLE_MANIFEST_NAME} must be a mapping (the bundle document without "
            "files[].content)")
    try:
        doc = json.loads(json.dumps(loaded, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        return AssemblyFailure(
            BUNDLE_SCHEMA_VIOLATION,
            f"{BUNDLE_MANIFEST_NAME} holds a value that is not a JSON value (a date or a "
            f"timestamp; quote it): {exc}")
    files = doc.get("files")
    if not isinstance(files, list):
        # The contract reports the missing / mistyped member with its own words.
        return copy.deepcopy(doc)
    findings: list[str] = []
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            continue
        where = f"files[{index}]"
        if "content" in entry:
            findings.append(
                f"{where}: {BUNDLE_MANIFEST_NAME} must not carry content; the text is read "
                "from the file logical_path names")
            continue
        logical = entry.get("logical_path")
        if not isinstance(logical, str):
            continue
        rel = plain_relative_path(logical)
        if rel is None:
            findings.append(
                f"{where}: logical_path {logical!r} must be a relative path with no empty, "
                "'.' or '..' segment")
            continue
        if logical in reserved_set:
            findings.append(
                f"{where}: logical_path {logical!r} names a host-placed tree file; a bundle "
                "file needs a name of its own")
            continue
        path, why = regular_file_under(root, rel)
        if path is None:
            findings.append(f"{where}: {logical} {why}")
            continue
        try:
            entry["content"] = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            findings.append(f"{where}: {logical} is not UTF-8 text")
        except OSError as exc:
            findings.append(f"{where}: {logical} cannot be read: {exc}")
    if findings:
        return AssemblyFailure(BUNDLE_SCHEMA_VIOLATION, "\n".join(findings))
    return doc


# --- snapshot -------------------------------------------------------------------------


def _walk_tree_files(root: Path) -> tuple[list[PurePosixPath], list[str]]:
    """Every regular file under `root`, relative, and every symbolic link (not followed)."""
    files: list[PurePosixPath] = []
    links: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in list(dirnames):
            if (base / name).is_symlink():
                links.append((base / name).relative_to(root).as_posix())
                dirnames.remove(name)
        for name in filenames:
            path = base / name
            rel = PurePosixPath(path.relative_to(root).as_posix())
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                links.append(rel.as_posix())
            elif stat.S_ISREG(info.st_mode):
                files.append(rel)
    return sorted(files), sorted(links)


def _copy_files(source: Path, target: Path, names: Iterable[PurePosixPath]) -> list[str]:
    copied: list[str] = []
    for rel in names:
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / rel, destination)
        copied.append(rel.as_posix())
    return copied


def snapshot_tree(paths: LineagePaths, host_placed: Iterable[str]) -> dict[str, list[str]]:
    """Copy the tree's final state into `snapshot/`: every regular file except the host-placed
    ones (`reserved_tree_names` minus the manifest — the seeds and the derived control file,
    which the host owns and re-places), so a seed the leaf edited is never copied back. A
    symbolic link is not followed and not copied. The snapshot directory is emptied first: a
    lineage is snapshotted when it ends, once. Returns `{"copied": [...], "links_refused":
    [...]}`."""
    skip = {str(n) for n in host_placed} - {BUNDLE_MANIFEST_NAME}
    if paths.snapshot.exists():
        shutil.rmtree(paths.snapshot)
    paths.snapshot.mkdir(parents=True)
    files, links = _walk_tree_files(paths.tree)
    kept = [rel for rel in files if rel.as_posix() not in skip]
    copied = _copy_files(paths.tree, paths.snapshot, kept)
    return {"copied": copied, "links_refused": links}


def seed_from_snapshot(new_paths: LineagePaths, old_paths: LineagePaths,
                       host_placed: Iterable[str]) -> list[str]:
    """Copy an ended lineage's snapshot into a new lineage's tree (the cold-`reuse` seed),
    after the new tree's own seeds are written. A host-placed name is skipped even if the
    snapshot holds it, and a file the new tree already holds is not overwritten — the new
    lineage's seeds are the host's current ones. Returns the copied names."""
    skip = {str(n) for n in host_placed} - {BUNDLE_MANIFEST_NAME}
    files, _links = _walk_tree_files(old_paths.snapshot)
    kept = [rel for rel in files
            if rel.as_posix() not in skip and not (new_paths.tree / rel).exists()]
    return _copy_files(old_paths.snapshot, new_paths.tree, kept)
