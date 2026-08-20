#!/usr/bin/env python3
"""Report on, and optionally delete, the durable isolated backend homes.

Since issue #64 each orchestration's isolated backend homes live at
`<homes-root>/<orchestration_id>/{claude,codex}`, where the root is
`~/.met-dsl/homes` (relocatable with `METDSL_WORKFLOW_HOMES_ROOT`). They hold the
ONLY record of what a leaf actually did — the claude transcript, the codex rollout —
so **nothing deletes them automatically and nothing ever will**. Retention is
indefinite, and this tool is the one way a home is removed.

That is a deliberate repeat of the decision `TODO.md` records for `workspace/`
retention: an automatic rule has to choose between deleting evidence of a run someone
may still audit and keeping everything, and the second is the only one that cannot
silently destroy the thing the directory exists for. This module is therefore invoked
by an operator and by nothing else; no conductor, hook, gate, or preflight calls it.

WHAT DELETION COSTS, so the operator can decide rather than discover:
  * `--resume` for that orchestration degrades to a COLD launch. Warm resume finds a
    leaf's session in the home's transcript; a rotated (re-created) home has none.
  * the run stops being auditable. `skills/workflow-audit-claude` /
    `workflow-audit-codex` / `workflow-timing-audit` all read from the home, and the
    artifacts under `workspace/orchestrations/<id>/` record what the host saw, not the
    leaf's own turn.

FAIL-CLOSED, in three independent places, because the argument is a directory path
under the operator's own home:
  1. the entry must be a real, non-symlinked directory owned by this uid;
  2. its `owner.json` must name a checkout, and that checkout's
     `orchestration_meta.json` must name this same orchestration id. A marker that is
     missing, unreadable, or inconsistent is `refused:unverifiable_owner`, and
     `--allow-unverifiable` overrides THAT refusal alone. The default is a refusal
     because a home with no marker may equally be a LIVE run belonging to a checkout
     this invocation cannot see;
  3. the orchestration's status, re-read under `_orchestration_meta_exclusive_lock` so
     a concurrent `--resume` cannot be observed mid-transition, must be terminal.
     `refused:orchestration_not_terminal` has NO override: a home deleted under a
     running leaf takes that leaf's live session with it.

Exit codes: 0 = the requested work was done; 2 = an orchestration named explicitly on
the command line was refused; 1 = usage or environment error.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

try:  # script run: sys.path[0] is tools/ ; package import: repo root on path
    from orchestration_runtime import (
        IDEMPOTENT_TERMINAL_STATUSES,
        WORKFLOW_HOME_OWNER_FILENAME,
        WORKFLOW_HOMES_ROOT_ENV,
        _orchestration_meta_exclusive_lock,
        _workflow_homes_root,
    )
except ModuleNotFoundError:  # pragma: no cover - import bootstrap for package execution
    from tools.orchestration_runtime import (
        IDEMPOTENT_TERMINAL_STATUSES,
        WORKFLOW_HOME_OWNER_FILENAME,
        WORKFLOW_HOMES_ROOT_ENV,
        _orchestration_meta_exclusive_lock,
        _workflow_homes_root,
    )

# `fail_closed` is terminal for this purpose and is not in `TERMINAL_STATUSES`:
# `IDEMPOTENT_TERMINAL_STATUSES` is the set that already means "this run is over" to
# the runtime, and reading it from there rather than respelling it is what keeps a
# status added later from silently becoming undeletable.
DELETABLE_STATUSES = IDEMPOTENT_TERMINAL_STATUSES

VERDICT_DELETABLE = "deletable"
REFUSED_NOT_A_DIRECTORY = "refused:not_a_directory"
REFUSED_FOREIGN_OWNER_UID = "refused:foreign_owner_uid"
REFUSED_UNVERIFIABLE_OWNER = "refused:unverifiable_owner"
REFUSED_NOT_TERMINAL = "refused:orchestration_not_terminal"


def _directory_size_bytes(path: Path) -> int:
    """Bytes under `path`, following no symlinks and surviving a racing deletion."""
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _read_owner_marker(entry: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads((entry / WORKFLOW_HOME_OWNER_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _orchestration_status(repo_root: Path, orchestration_id: str) -> str | None:
    """The owner checkout's recorded status, read under the metadata lock.

    The LOCK is the point, not the read: `update_orchestration_status` writes the
    terminal status inside this same lock, so an unlocked read can observe the file
    between a `--resume`'s status write and whatever follows it. Returns None when the
    metadata is absent or unreadable, which the caller treats as unverifiable rather
    than as "not running".
    """
    meta_path = repo_root / "workspace" / "orchestrations" / orchestration_id / "orchestration_meta.json"
    try:
        with _orchestration_meta_exclusive_lock(repo_root, orchestration_id):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    status = meta.get("status")
    return status if isinstance(status, str) else None


def inspect_entry(entry: Path, orchestration_id: str) -> dict[str, Any]:
    """Everything the operator needs about one `<homes-root>/<oid>/` directory."""
    report: dict[str, Any] = {
        "orchestration_id": orchestration_id,
        "path": str(entry),
        "owner_repo_root": "",
        "status": "",
        "backends": [],
        "size_bytes": 0,
        "verdict": "",
    }
    try:
        info = entry.lstat()
    except OSError:
        report["verdict"] = REFUSED_NOT_A_DIRECTORY
        return report
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        report["verdict"] = REFUSED_NOT_A_DIRECTORY
        return report
    if info.st_uid != os.getuid():
        report["verdict"] = REFUSED_FOREIGN_OWNER_UID
        return report
    try:
        report["backends"] = sorted(
            child.name for child in entry.iterdir() if child.is_dir() and not child.is_symlink())
    except OSError:
        report["backends"] = []
    report["size_bytes"] = _directory_size_bytes(entry)
    marker = _read_owner_marker(entry)
    if not marker or marker.get("orchestration_id") != orchestration_id:
        report["verdict"] = REFUSED_UNVERIFIABLE_OWNER
        return report
    raw_repo = marker.get("repo_root")
    if not isinstance(raw_repo, str) or not raw_repo.strip():
        report["verdict"] = REFUSED_UNVERIFIABLE_OWNER
        return report
    repo_root = Path(raw_repo.strip())
    report["owner_repo_root"] = str(repo_root)
    status = _orchestration_status(repo_root, orchestration_id)
    if status is None:
        # The marker is a LOCATOR, and here it located nothing: the checkout was moved
        # or removed. Whether the run finished is then unanswerable, so this is the
        # unverifiable case rather than the not-terminal one — an operator who knows
        # the checkout is gone can say so with --allow-unverifiable.
        report["verdict"] = REFUSED_UNVERIFIABLE_OWNER
        return report
    report["status"] = status
    report["verdict"] = (
        VERDICT_DELETABLE if status in DELETABLE_STATUSES else REFUSED_NOT_TERMINAL)
    return report


def _delete_entry(entry: Path, homes_root: Path) -> None:
    """Remove one orchestration directory, after proving it is inside the root.

    The containment assert is not decoration. Every other check above answers a
    question about the directory's CONTENT; this one answers where the path landed,
    which is the question a symlink or a crafted orchestration id would attack, and it
    is the last thing that runs before `rmtree`.
    """
    resolved = entry.resolve()
    root_resolved = homes_root.resolve()
    if resolved == root_resolved or root_resolved not in resolved.parents:
        raise ValueError(f"refusing to delete a path outside the homes root: {resolved}")
    shutil.rmtree(resolved)


def prune(homes_root: Path, *, orchestration_ids: list[str] | None, delete: bool,
          allow_unverifiable: bool) -> tuple[list[dict[str, Any]], int]:
    """Inspect (and optionally delete) entries; returns the reports and an exit code."""
    explicit = list(orchestration_ids or [])
    if explicit:
        names = explicit
    else:
        try:
            names = sorted(child.name for child in homes_root.iterdir())
        except OSError:
            return [], 0
    reports: list[dict[str, Any]] = []
    refused_explicit = False
    for name in names:
        report = inspect_entry(homes_root / name, name)
        deletable = report["verdict"] == VERDICT_DELETABLE or (
            allow_unverifiable and report["verdict"] == REFUSED_UNVERIFIABLE_OWNER)
        report["deleted"] = False
        if deletable and delete:
            try:
                _delete_entry(homes_root / name, homes_root)
            except (OSError, ValueError) as exc:
                report["verdict"] = f"refused:delete_failed:{exc}"
            else:
                report["deleted"] = True
        if not deletable and name in explicit:
            refused_explicit = True
        reports.append(report)
    return reports, (2 if refused_explicit else 0)


def _render_text(reports: list[dict[str, Any]], *, delete: bool, homes_root: Path) -> str:
    lines = [f"homes root: {homes_root}"]
    if not reports:
        lines.append("(no isolated backend homes)")
        return "\n".join(lines)
    for report in reports:
        # Scaled rather than always-MB: a real home is tens of megabytes, but a run that
        # died at its first leaf is a few kilobytes, and "0.0 MB" reads as "this entry is
        # empty, deleting it costs nothing" — the opposite of what the report is for.
        size = report["size_bytes"]
        if size >= 1024 * 1024:
            size_s = f"{size / (1024 * 1024):.1f} MB"
        elif size >= 1024:
            size_s = f"{size / 1024:.1f} KB"
        else:
            size_s = f"{size} B"
        action = "DELETED" if report.get("deleted") else ("would delete" if
                 report["verdict"] == VERDICT_DELETABLE and not delete else report["verdict"])
        lines.append(
            f"{report['orchestration_id']}  status={report['status'] or '?'}  "
            f"backends={','.join(report['backends']) or '-'}  {size_s}  {action}")
        if report["owner_repo_root"]:
            lines.append(f"    owner: {report['owner_repo_root']}")
    if not delete:
        lines.append("")
        lines.append("Report only. Re-run with --delete to remove the entries marked "
                     "'would delete'.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report on, and optionally delete, durable isolated backend homes.")
    parser.add_argument("--homes-root", default="",
                        help=f"override the homes root (default: ${WORKFLOW_HOMES_ROOT_ENV} "
                             "or ~/.met-dsl/homes)")
    parser.add_argument("--orchestration-id", action="append", default=[],
                        help="limit to this orchestration id (repeatable)")
    parser.add_argument("--all", action="store_true",
                        help="operate on every entry under the homes root")
    parser.add_argument("--delete", action="store_true",
                        help="actually remove the entries; without it this reports only")
    parser.add_argument("--allow-unverifiable", action="store_true",
                        help="also delete entries whose owner.json is missing or "
                             "inconsistent (never overrides a non-terminal refusal)")
    parser.add_argument("--json", action="store_true", help="emit the reports as JSON")
    args = parser.parse_args(argv)

    if args.orchestration_id and args.all:
        parser.error("--orchestration-id and --all are mutually exclusive")
    if not args.orchestration_id and not args.all:
        parser.error("pass --all or at least one --orchestration-id; there is no "
                     "default scope, because deleting a home destroys the only record "
                     "of what its leaves did")
    homes_root = Path(args.homes_root).expanduser() if args.homes_root else _workflow_homes_root()
    reports, code = prune(
        homes_root,
        orchestration_ids=list(args.orchestration_id) or None,
        delete=bool(args.delete),
        allow_unverifiable=bool(args.allow_unverifiable),
    )
    if args.json:
        print(json.dumps({"homes_root": str(homes_root), "entries": reports}, indent=2))
    else:
        print(_render_text(reports, delete=bool(args.delete), homes_root=homes_root))
    return code


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
