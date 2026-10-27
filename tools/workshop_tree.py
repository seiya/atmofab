"""The work tree of a `workshop leaf` lineage: its paths, its seeds, its record, its snapshot.

`docs/design/producer_workshop_leaf.md` decides the shape (issue #534): a PRODUCING leaf works
in a host-seeded tree, and the host copies the tree's final state into the existing document
contract after the leaf exits — the `CodegenBundle` of `generate.generate`
(`assemble_bundle_document`), or the `{ir, compile_fail}` document of `compile.generate`
(`assemble_ir_document`, step 2). This module is the host's half of that copy and
of the lineage's bookkeeping. It holds no leaf launch and no gate: the document it assembles goes
through the unchanged `codegen_bundle.pure_bundle_contract_violation`, and every artifact a later
`phase` reads is still written by the conductor from that document.

Two places per lineage. The leaf's CLI sees only the first (its `config/` and `tree/`; a
program the wrapper runs for it sees `tree/` and, for `run`, `staged/bin`), and nothing
leaf-side sees the second:

* the LINEAGE HOME, `<homes-root>/<oid>/<backend>/<lineage_id>/` (outside the checkout, like
  the codex lineage home of issue #245): `config/` (the CLI's private config home), `tree/` (the
  work tree), `staged/closure` (the staged dependency closure's sources and headers),
  `staged/obj` + `staged/bin` (the wrapper's object and binary directories, made fresh by each
  `compile`), `syntax/` (syntax-check stage directories) and
  `snapshot/` (the tree's non-host-placed files, copied after each launch of the lineage);
* the LINEAGE RECORD, `workspace/orchestrations/<oid>/workshop/<lineage_id>/`, host-written
  only: `workshop_lineage.json`, `command_log.jsonl` (every build-runtime call of the wrapper) and
  `wrapper_calls.jsonl` (every request the wrapper's transport, `tools/workshop_mcp.py`,
  served; a CLI lineage's) or `loop_turns.jsonl` (the host loop's record of an HTTP lineage,
  `tools/llm_workshop_loop.py`). An HTTP lineage's home has no `config/`.

The HTTP host loop's two write tools reach the tree through `write_tree_file` /
`edit_tree_file`, which resolve every parent through its parent's fd with `O_NOFOLLOW` and
refuse a host-placed name.

No function here raises on what the leaf wrote: a malformed tree is a finding the caller
reports back, never an exception. Neutral core (`docs/BACKEND_BOUNDARY.md`): the names of the
seeds and of the control file are the caller's.
"""

from __future__ import annotations

import copy
import errno
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

#: The IR a `compile.generate` leaf writes at the tree root, and the declaration it writes in its
#: place when the phase cannot be completed from its inputs (issue #534 step 2).
IR_DOCUMENT_NAME = "spec.ir.yaml"
COMPILE_FAIL_NAME = "compile_fail.json"

#: The category of a tree that holds no output file at all (`docs/design/
#: producer_workshop_leaf.md` §Output files in the tree).
WORKSHOP_TREE_EMPTY = "workshop_tree_empty"
#: The category of every other assembly failure — the pure producer's own malformed-bundle
#: category, so the routing of a defective document does not depend on its transport.
BUNDLE_SCHEMA_VIOLATION = "bundle_schema_violation"
#: The IR producer's own malformed-document category (`COMPILE_IR_DOCUMENT_VIOLATION`).
IR_DOCUMENT_VIOLATION = "ir_document_violation"

#: Why a lineage ended. `None` while it is open. `superseded`: the host continued its work in
#: a new lineage because its session could not be resumed (issue #534 PR-3) — seeded from its
#: snapshot when the home still holds one — so nothing will continue it. `declared_fail`: the
#: leaf's tree declared the phase uncompletable (`compile_fail.json`, issue #534 step 2), the
#: run stops there, and no lineage follows.
ENDED_BY_VALUES: tuple[str, ...] = (
    "leaf_exit", "max_wrapper_calls", "lineage_timeout", "leaf_timeout", "superseded",
    "declared_fail")

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: The transports a lineage home is prepared for: the directory under `<homes-root>/<oid>/`
#: that holds it, which is `orchestration_runtime.WORKFLOW_BACKEND_HOME_DIRNAMES` (the prune tool
#: walks those names), and the value `pure_leaf.WORKSHOP_LINEAGE_BACKENDS` maps the launching
#: provider to: `claude` (`claude_cli`, issue #534 step 1), `codex` (`codex_cli`, step 2 PR-2),
#: `http` (both HTTP providers, whose host loop is one transport — PR-J3-b; no `config/`).
LINEAGE_BACKENDS: tuple[str, ...] = ("claude", "codex", "http")

_LINEAGE_LOCKS: dict[str, threading.Lock] = {}
_LINEAGE_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class LineagePaths:
    """Every path of one lineage. `homes_root` is `operator_private_root.workflow_homes_root()`
    in production; `orchestration_dir` is `workspace/orchestrations/<oid>`; `backend` is the
    CLI's (`LINEAGE_BACKENDS`). `config` is that CLI's private home in both: the claude
    lineage's `CLAUDE_CONFIG_DIR`, the codex lineage's `CODEX_HOME`."""

    homes_root: Path
    orchestration_id: str
    lineage_id: str
    orchestration_dir: Path
    backend: str = "claude"

    def __post_init__(self) -> None:
        for name in ("orchestration_id", "lineage_id"):
            value = getattr(self, name)
            if not (isinstance(value, str) and _SAFE_ID_RE.fullmatch(value)):
                raise ValueError(
                    f"lineage {name} must be a plain [A-Za-z0-9_-] token (got {value!r})")
        if self.backend not in LINEAGE_BACKENDS:
            raise ValueError(f"lineage backend must be one of {list(LINEAGE_BACKENDS)} (got "
                             f"{self.backend!r})")

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
    def staged_closure(self) -> Path:
        """The staged dependency closure's sources and headers, placed once per lineage. Bound
        into no run: each `compile` copies them into a fresh `staged_obj`."""
        return self.staged / "closure"

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
        """Create the home's work directories, 0700 (all but `config/`, which the CLI home's own
        preparer owns — issue #534 PR-2's) and the record directory. The home itself must
        already exist: its preparer creates it exclusively, 0700, and it is never created
        here."""
        if not self.home.is_dir():
            raise ValueError(f"lineage home {self.home} does not exist")
        for directory in (self.tree, self.staged, self.staged_closure, self.staged_obj,
                          self.staged_bin, self.syntax, self.snapshot):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        # The record directory (`<orchestration>/workshop/<lineage>/`), one level at a time
        # with a chmod after each mkdir: a `parents=True` mkdir under a umask that holds owner
        # bits (legal, if unusual) creates an intermediate the next level cannot be made in,
        # and the launch dies. A level that already exists keeps its mode.
        missing: list[Path] = []
        directory = self.record_dir
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)


def reserved_tree_names(seed_names: Iterable[str], control_file_basename: str | None,
                        outputs: Iterable[str] = (BUNDLE_MANIFEST_NAME,)) -> frozenset[str]:
    """The tree paths a bundle may not name as one of its files: every seed, the derived
    control file (a lineage that derives one) and the producer's own output files (`bundle.yaml`;
    `spec.ir.yaml` and `compile_fail.json` on an IR lineage). A bundle naming one is refused at
    `derive` and at the snapshot alike, because the host — or the assembly — reads what these
    names hold as something other than a bundle file."""
    names = {str(n) for n in seed_names}
    if control_file_basename:
        names.add(str(control_file_basename))
    names.update(str(n) for n in outputs)
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


def record_last_launch(paths: LineagePaths, *, agent_run_id: str, rc: int | None,
                       ended_at_utc: str) -> None:
    """Record the lineage's latest launch — its id, the leaf's return code and when it ended —
    in `workshop_lineage.json#last_launch`. `ended_by` stays null after a clean exit (the lineage
    is open for a warm reopen), so this is what tells a reader whether the last launch of an
    open lineage ended cleanly."""
    with _Locked(paths):
        doc = read_lineage_record(paths)
        doc["last_launch"] = {"agent_run_id": agent_run_id, "rc": rc,
                              "ended_at_utc": ended_at_utc}
        _write_json_atomic(paths.record_json, doc)


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


def _open_no_follow(root: Path, rel: PurePosixPath) -> int:
    """An fd open for reading on `<root>/<rel>`, reached through no symbolic link: each
    directory component is opened relative to the previous one with `O_NOFOLLOW`, and the file
    itself too, so a component swapped for a link between a check and the open (a run of the
    leaf's own program still writing the tree) is refused rather than followed. Raises
    `OSError` (`ELOOP` for a link) and `ValueError` (a NUL in the name)."""
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in rel.parts[:-1]:
            nxt = os.open(part, flags_dir, dir_fd=fd)
            os.close(fd)
            fd = nxt
        file_fd = os.open(rel.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)
    return file_fd


def read_regular_file_under(root: Path, rel: PurePosixPath) -> tuple[bytes | None, str | None]:
    """`(bytes, None)` when `<root>/<rel>` is a regular file reached through no symbolic link,
    else `(None, why)`. A link anywhere on the way is refused, whether it points in or out of
    the tree (a program the wrapper ran can create one in the read-write tree). Never raises."""
    try:
        fd = _open_no_follow(Path(root), rel)
    except FileNotFoundError:
        return None, "does not exist"
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None, "is reached through a symbolic link"
        if exc.errno == errno.ENOTDIR:
            # `O_DIRECTORY | O_NOFOLLOW` on a link to a directory answers ENOTDIR too: tell the
            # two apart by what the component is (the message only; the open already refused).
            current = Path(root)
            for part in rel.parts[:-1]:
                current = current / part
                if current.is_symlink():
                    return None, "is reached through a symbolic link"
            return None, "is not reachable (a component is not a directory)"
        return None, f"cannot be read ({exc.strerror or exc})"
    except ValueError as exc:
        return None, f"is not a usable path ({exc})"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "is not a regular file"
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(), None
    except OSError as exc:
        return None, f"cannot be read ({exc.strerror or exc})"
    finally:
        os.close(fd)


def write_host_file(root: Path, name: str, text: str) -> None:
    """Write a host-placed file (the derived control file) at the top of `root`, never through
    a link: whatever the leaf left at that name — a link, a directory — is removed first and
    the file is created with `O_NOFOLLOW`, so the host's write lands in the tree and nowhere
    else."""
    target = Path(root) / name
    try:
        info = target.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and not stat.S_ISREG(info.st_mode):
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(target)
        else:
            target.unlink()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)


def _open_parent_creating(root: Path, rel: PurePosixPath) -> int:
    """An fd on `<root>/<rel>`'s parent directory, reached through no symbolic link and created
    under `root` where missing: each component is opened `O_DIRECTORY | O_NOFOLLOW` relative to
    the previous fd, and a missing one is made (0700) through that fd, never through a path
    string, so a component the leaf replaced by a link between two calls is refused. Raises
    `OSError` (`ELOOP` / `ENOTDIR` for a link or a file on the way)."""
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in rel.parts[:-1]:
            try:
                nxt = os.open(part, flags_dir, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                nxt = os.open(part, flags_dir, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_tree_file(root: Path, name: str, text: str, *,
                    reserved: Iterable[str]) -> str | None:
    """Write a LEAF's file at `<root>/<name>` for the HTTP host loop's `write_file` (issue #534
    PR-J3-b): None when written, else the finding the leaf is answered with. Never raises.

    `name` must be a plain relative path (`plain_relative_path`) and not a `reserved` name (the
    lineage's host-placed files: the seeds, `CONTRACT.md`, the control file — on a CLI leaf the
    host restores such a write silently; here it is refused, because the host is the one
    writing). Every parent is opened through its parent's fd with `O_NOFOLLOW` and created there
    when missing; the file itself is opened `O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW`, 0644,
    through the parent's fd. So nothing reached through a link is written, whether the link
    points in or out of the tree."""
    rel = plain_relative_path(name)
    if rel is None:
        return (f"{name!r} is not a plain relative path of your work tree (no leading `/`, no "
                "`.` or `..` segment, no backslash, no empty segment)")
    if rel.as_posix() in {str(n) for n in reserved}:
        return (f"{rel.as_posix()} is a host-placed file (a seed or CONTRACT.md) and is "
                "read-only; write your own file under another name")
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        return f"{rel.as_posix()}: content is not encodable as UTF-8 ({exc.reason})"
    try:
        parent = _open_parent_creating(Path(root), rel)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return f"{rel.as_posix()}: a directory on its path is a symbolic link or a file"
        return f"{rel.as_posix()}: cannot be written ({exc.strerror or exc})"
    except ValueError as exc:
        return f"{rel.as_posix()}: is not a usable path ({exc})"
    try:
        try:
            # `O_NONBLOCK`: a FIFO a wrapper-run program left at the name answers ENXIO
            # rather than holding the loop open waiting for a reader.
            fd = os.open(rel.parts[-1],
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o644, dir_fd=parent)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return f"{rel.as_posix()}: is a symbolic link; it is not written through"
            if exc.errno == errno.EISDIR:
                return f"{rel.as_posix()}: is a directory"
            return f"{rel.as_posix()}: cannot be written ({exc.strerror or exc})"
        except ValueError as exc:
            return f"{rel.as_posix()}: is not a usable path ({exc})"
        with os.fdopen(fd, "wb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return f"{rel.as_posix()}: is not a regular file"
            stream.write(data)
    except OSError as exc:
        return f"{rel.as_posix()}: cannot be written ({exc})"
    finally:
        os.close(parent)
    return None


def edit_tree_file(root: Path, name: str, old: str, new: str, *,
                   reserved: Iterable[str]) -> str | None:
    """Replace the ONE occurrence of `old` in the leaf's file `<root>/<name>` with `new`, for
    the HTTP host loop's `edit_file`: None when done, else the finding. Read through
    `read_regular_file_under`, written through `write_tree_file` (the reserved set and the
    no-link rule are that function's). Zero occurrences, or two and more, are refused with the
    count. Never raises."""
    rel = plain_relative_path(name)
    if rel is None:
        return write_tree_file(root, name, "", reserved=reserved)
    if rel.as_posix() in {str(n) for n in reserved}:
        return write_tree_file(root, name, "", reserved=reserved)
    if not old:
        return "old_text is empty; name the exact text to replace"
    data, why = read_regular_file_under(Path(root), rel)
    if data is None:
        return f"{rel.as_posix()}: {why}"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"{rel.as_posix()}: is not UTF-8 text"
    count = text.count(old)
    if count != 1:
        return (f"{rel.as_posix()}: old_text occurs {count} times; it must occur exactly once "
                "(give more surrounding text, or write the file whole)")
    return write_tree_file(root, name, text.replace(old, new, 1), reserved=reserved)


def list_tree_files(root: Path) -> tuple[list[tuple[str, int]], list[str]]:
    """`([(path, bytes)], [link paths])` of `root`, for the HTTP host loop's `list_files`:
    every regular file (`_walk_tree_files`, links not followed) with its size, sorted."""
    files, links = _walk_tree_files(Path(root))
    out: list[tuple[str, int]] = []
    for rel in files:
        try:
            out.append((rel.as_posix(), (Path(root) / rel).lstat().st_size))
        except OSError:
            continue
    return out, links


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
    try:
        return _assemble(Path(root), frozenset(reserved))
    except (ValueError, UnicodeError, RecursionError, OSError) as exc:
        # A manifest no reader below anticipated — a NUL or a lone surrogate in a path, a
        # nesting deep enough to exhaust the parser — is still the tree's defect, not the host's.
        return AssemblyFailure(BUNDLE_SCHEMA_VIOLATION,
                               f"{BUNDLE_MANIFEST_NAME} cannot be assembled: "
                               f"{type(exc).__name__}: {exc}")


def _assemble(root: Path, reserved_set: frozenset[str]) -> dict[str, Any] | AssemblyFailure:
    raw, why = read_regular_file_under(root, PurePosixPath(BUNDLE_MANIFEST_NAME))
    if raw is None:
        if why == "does not exist":
            return AssemblyFailure(
                WORKSHOP_TREE_EMPTY,
                f"the tree holds no {BUNDLE_MANIFEST_NAME}: write the bundle manifest at the "
                "tree root, naming every bundle file")
        return AssemblyFailure(BUNDLE_SCHEMA_VIOLATION, f"{BUNDLE_MANIFEST_NAME} {why}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return AssemblyFailure(BUNDLE_SCHEMA_VIOLATION, f"{BUNDLE_MANIFEST_NAME} is not UTF-8")
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
        data, why = read_regular_file_under(root, rel)
        if data is None:
            findings.append(f"{where}: {logical} {why}")
            continue
        try:
            entry["content"] = data.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(f"{where}: {logical} is not UTF-8 text")
    if findings:
        return AssemblyFailure(BUNDLE_SCHEMA_VIOLATION, "\n".join(findings))
    return doc


def assemble_ir_document(root: Path, reserved: Iterable[str] = ()) -> dict[str, Any] | AssemblyFailure:
    """The `{ir, compile_fail}` document a `compile.generate` tree holds (issue #534 step 2), or
    the `AssemblyFailure` that stops it. `root` is the work tree (the wrapper's `check_ir`) or the
    lineage's snapshot (the hand-off); `reserved` is accepted for the assembler's common shape
    and unused (an IR names no file).

    `spec.ir.yaml` is `yaml.safe_load`ed into `ir`; `compile_fail.json` is `json.loads`ed into
    `compile_fail`. Each absent file contributes `None`. BOTH present gives a document carrying
    both, which the producer's contract (`Conductor._pure_ir_document_violations`, unchanged)
    refuses as two answers; NEITHER is `workshop_tree_empty`. A file reached through a link,
    not a regular file, not UTF-8, not parseable, or holding a value that is not a JSON value
    is an `ir_document_violation` finding. What passes goes through the same contract a pure
    reply goes through, which owns every other rule. Never raises on the tree's content."""
    try:
        return _assemble_ir(Path(root))
    except (ValueError, UnicodeError, RecursionError, OSError) as exc:
        return AssemblyFailure(IR_DOCUMENT_VIOLATION,
                               f"the IR cannot be assembled: {type(exc).__name__}: {exc}")


def _read_tree_document(root: Path, name: str,
                        parse: Any) -> tuple[bool, Any, str | None]:
    """`(present, value, finding)` for one output file of the tree."""
    raw, why = read_regular_file_under(root, PurePosixPath(name))
    if raw is None:
        if why == "does not exist":
            return False, None, None
        return True, None, f"{name} {why}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return True, None, f"{name} is not UTF-8"
    try:
        value = parse(text)
    except (yaml.YAMLError, ValueError) as exc:
        return True, None, f"{name} cannot be parsed: {exc}"
    try:
        value = json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        return True, None, (f"{name} holds a value that is not a JSON value (a date or a "
                            f"timestamp; quote it): {exc}")
    return True, value, None


def _assemble_ir(root: Path) -> dict[str, Any] | AssemblyFailure:
    has_ir, ir, ir_why = _read_tree_document(root, IR_DOCUMENT_NAME, yaml.safe_load)
    has_fail, declared, fail_why = _read_tree_document(root, COMPILE_FAIL_NAME, json.loads)
    if not has_ir and not has_fail:
        return AssemblyFailure(
            WORKSHOP_TREE_EMPTY,
            f"the tree holds neither {IR_DOCUMENT_NAME} nor {COMPILE_FAIL_NAME}: write the IR "
            f"at the tree root as {IR_DOCUMENT_NAME}")
    findings = [f for f in (ir_why, fail_why) if f]
    if has_fail and fail_why is None and declared is None:
        # A file that declares nothing is not the absence of a declaration: the contract says
        # a `compile_fail.json` beside an IR is refused, and a `null` one would slip through it.
        findings.append(f"{COMPILE_FAIL_NAME} holds null: delete it, or write the declaration "
                        "object")
    if findings:
        return AssemblyFailure(IR_DOCUMENT_VIOLATION, "\n".join(findings))
    if has_ir and not isinstance(ir, dict):
        return AssemblyFailure(
            IR_DOCUMENT_VIOLATION,
            f"{IR_DOCUMENT_NAME} must be a YAML mapping holding the IR's top-level sections; "
            f"got {type(ir).__name__}")
    return {"ir": ir, "compile_fail": declared}


# --- the reading kind (issue #534 step 3) ---------------------------------------------


def assemble_document_file(root: Path, name: str,
                           category: str) -> dict[str, Any] | AssemblyFailure:
    """The one JSON object a READING lineage writes at its tree root (`verdict.json`,
    `review.json`), or the `AssemblyFailure` that stops it. A reviewer's document has one
    category for every defect, the reviewer's own schema category (`category`), so an absent
    file, a link, a file that is not UTF-8, not JSON or not an object all come back as that
    category with a finding, and the reviewer loop's repair turn handles them as it handles a
    malformed reply. Never raises on the tree's content."""
    try:
        present, value, why = _read_tree_document(Path(root), name, json.loads)
    except (ValueError, UnicodeError, RecursionError, OSError) as exc:
        return AssemblyFailure(category, f"{name} cannot be read: {type(exc).__name__}: {exc}")
    if not present:
        return AssemblyFailure(
            category, f"no `{name}` in the tree: write your document at the tree root as {name}")
    if why is not None:
        return AssemblyFailure(category, why)
    if not isinstance(value, dict):
        return AssemblyFailure(
            category, f"{name} must hold one JSON object; got {type(value).__name__}")
    return value


def expand_bundle_document(document: Mapping[str, Any],
                           manifest_name: str = BUNDLE_MANIFEST_NAME) -> dict[str, str]:
    """The tree files a CodegenBundle document is seeded as for the reviewer that judges it: the
    document without `files[].content` as YAML under `manifest_name`, and each file's text at
    its `logical_path` — the producer's own tree contract read back, so the reviewer reads
    source rather than JSON string escapes. The inverse of `assemble_bundle_document` (which
    reads the manifest from `BUNDLE_MANIFEST_NAME`). Raises `ValueError` on a document the host
    cannot expand (a `logical_path` that is not a plain relative path, that names the manifest
    or that two entries share, a `content` that is not a string): the document is a certified
    host artifact, so that is a host defect, not a finding."""
    if not isinstance(document, Mapping):
        raise ValueError("the bundle document is not a mapping")
    manifest = copy.deepcopy(dict(document))
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("the bundle document carries no files[] list")
    out: dict[str, str] = {}
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise ValueError(f"files[{index}] is not a mapping")
        logical = entry.get("logical_path")
        content = entry.pop("content", None)
        if not isinstance(logical, str) or plain_relative_path(logical) is None:
            raise ValueError(f"files[{index}]: logical_path {logical!r} is not a plain "
                             "relative path")
        if not isinstance(content, str):
            raise ValueError(f"files[{index}]: {logical} carries no text content")
        if logical == manifest_name or logical in out:
            raise ValueError(f"files[{index}]: logical_path {logical!r} names the manifest or "
                             "another file of the bundle")
        out[logical] = content
    out[manifest_name] = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True)
    return out


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
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                links.append(rel.as_posix())
            elif stat.S_ISREG(info.st_mode):
                files.append(rel)
    return sorted(files), sorted(links)


def _copy_files(source: Path, target: Path,
                names: Iterable[PurePosixPath]) -> tuple[list[str], list[str]]:
    """Copy each named file of `source` to the same path under `target`, read through no link
    (`read_regular_file_under`). Returns `(copied, refused)`: a file that is a link by the time
    it is read, or cannot be read, is refused rather than raised."""
    copied: list[str] = []
    refused: list[str] = []
    for rel in names:
        data, why = read_regular_file_under(source, rel)
        if data is None:
            refused.append(f"{rel.as_posix()}: {why}")
            continue
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        copied.append(rel.as_posix())
    return copied, refused


def snapshot_tree(paths: LineagePaths, host_placed: Iterable[str]) -> dict[str, list[str]]:
    """Copy the tree's final state into `snapshot/`: every regular file except the host-placed
    ones (`reserved_tree_names` minus the manifest — the seeds and the derived control file,
    which the host owns and re-places), so a seed the leaf edited is never copied back. A
    symbolic link is not followed and not copied, and a file that cannot be read is refused,
    never raised. The snapshot directory is emptied first: the conductor snapshots a lineage
    after each of its launches, and each snapshot replaces the previous one. Returns `{"copied": [...], "links_refused": [...], "unreadable": [...]}`."""
    skip = {str(n) for n in host_placed} - {BUNDLE_MANIFEST_NAME}
    if paths.snapshot.exists():
        shutil.rmtree(paths.snapshot)
    paths.snapshot.mkdir(parents=True)
    files, links = _walk_tree_files(paths.tree)
    kept = [rel for rel in files if rel.as_posix() not in skip]
    copied, unreadable = _copy_files(paths.tree, paths.snapshot, kept)
    return {"copied": copied, "links_refused": links, "unreadable": unreadable}


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
    copied, _refused = _copy_files(old_paths.snapshot, new_paths.tree, kept)
    return copied
