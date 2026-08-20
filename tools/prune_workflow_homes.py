#!/usr/bin/env python3
"""Report on, and optionally delete, the durable isolated backend homes.

Since issue #64 each orchestration's isolated backend homes live at
`<homes-root>/<orchestration_id>/{claude,codex}`, where the root is
`~/.met-dsl/homes` (relocatable with `METDSL_WORKFLOW_HOMES_ROOT`). They hold the
ONLY record of what a leaf actually did — the claude transcript, the codex rollout —
so **nothing deletes them automatically and nothing ever will**. Retention is
indefinite, and this tool is the one way a home is removed.

The reason is that an automatic rule has to choose between deleting evidence of a run
someone may still audit and keeping everything, and the second is the only one that
cannot silently destroy the thing the directory exists for. This module is therefore
invoked by an operator and by nothing else; no conductor, hook, gate, or preflight
calls it.

`TODO.md` is cited here for what it ACTUALLY says, which is not what an earlier version
of this docstring claimed. Its `workspace/` entry is an OPEN high defect — "~14 GB of
run artifacts carry no retention rule", and the loss of a pinned directory has already
turned one test into a permanent skip — whose fix direction is "state a retention rule
in `docs/RUNBOOK.md`". So the parallel is not "that entry decided against automation";
it is that the remedy that entry ASKS FOR is what this pair does: a rule written down
where an operator reads it, and a tool that carries it out. Nothing here is evidence
that leaving `workspace/` unruled is fine.

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

Exit codes: **0 = the requested work was done, 2 = it was not** — either an orchestration
named explicitly on the command line was refused, or argparse rejected the invocation.
There is deliberately no third code: the two are distinguished by the output, and a
caller that must branch should use `--json`, whose `entries[].verdict` says which refusal
it was. An earlier version of this docstring promised `1 = usage or environment error`
and the module never returned 1, so a script following it would have treated a live-home
refusal as a bug in its own flags.

A homes root that cannot be listed at all is a third thing and is reported as such rather
than as an empty root, because "(no isolated backend homes)" and "I could not look" are
the same sentence otherwise.
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
    import orchestration_runtime as _runtime
    from orchestration_runtime import (
        IDEMPOTENT_TERMINAL_STATUSES,
        WORKFLOW_BACKEND_HOME_DIRNAMES,
        WORKFLOW_HOME_OWNER_FILENAME,
        WORKFLOW_HOMES_ROOT_ENV,
        _is_safe_path_id,
        _orchestration_meta_exclusive_lock,
    )
except ModuleNotFoundError:  # pragma: no cover - import bootstrap for package execution
    from tools import orchestration_runtime as _runtime
    from tools.orchestration_runtime import (
        IDEMPOTENT_TERMINAL_STATUSES,
        WORKFLOW_BACKEND_HOME_DIRNAMES,
        WORKFLOW_HOME_OWNER_FILENAME,
        WORKFLOW_HOMES_ROOT_ENV,
        _is_safe_path_id,
        _orchestration_meta_exclusive_lock,
    )


def _workflow_homes_root() -> Path:
    """The homes root, resolved through the MODULE rather than a bound function object.

    `from orchestration_runtime import _workflow_homes_root` binds the function at import
    time, so anything that later replaces the module attribute — the suite's guard in
    `tools/tests/conftest.py`, which exists to stop a test writing into the operator's
    real `~/.met-dsl` — does not reach this module at all. Measured: with the guard
    installed and the redirect cleared, `prune --all` resolved the operator's real root
    and the guard never fired. Report-only, so nothing was removed, but the ONE module
    that deletes was the one outside the guard, and the conftest docstring claimed it
    wrapped "the ONE function that decides where a home goes".
    """
    return _runtime._workflow_homes_root()

# `fail_closed` is terminal for this purpose and is not in `TERMINAL_STATUSES`:
# `IDEMPOTENT_TERMINAL_STATUSES` is the set that already means "this run is over" to
# the runtime, and reading it from there rather than respelling it is what keeps a
# status added later from silently becoming undeletable.
DELETABLE_STATUSES = IDEMPOTENT_TERMINAL_STATUSES

VERDICT_DELETABLE = "deletable"
REFUSED_NOT_A_BACKEND_HOME = "refused:not_a_backend_home"
REFUSED_INVALID_ORCHESTRATION_ID = "refused:invalid_orchestration_id"
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
    # EXISTENCE FIRST, LOCK SECOND. `_orchestration_meta_exclusive_lock` creates its
    # lock file, and `_fcntl_exclusive_lock` `mkdir(parents=True)`s the directory to put
    # it in — so taking the lock before knowing the metadata is there made a
    # REPORT-ONLY run write `workspace/orchestrations/<oid>/orchestration_meta.json.lock`
    # into whatever path the marker named, which after a moved or deleted checkout is an
    # unrelated project. A missing file has nothing to read under a lock anyway: there is
    # no state for a concurrent `--resume` to be caught mid-transition in, and the answer
    # is the same None either way. The TOCTOU that remains — the file appearing between
    # this check and the lock — falls to the unverifiable side, which is fail-closed.
    if not meta_path.is_file():
        return None
    try:
        with _orchestration_meta_exclusive_lock(repo_root, orchestration_id):
            return _read_status_locked(repo_root, orchestration_id)
    except (OSError, ValueError):
        return None


def _read_status_locked(repo_root: Path, orchestration_id: str) -> str | None:
    """The recorded status, read with the caller ALREADY holding the metadata lock.

    Split out so the delete path can re-read it inside one lock held across the check and
    the `rmtree`, rather than taking the lock twice with a window in between.
    """
    meta_path = repo_root / "workspace" / "orchestrations" / orchestration_id / "orchestration_meta.json"
    try:
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
    # WHAT THE ENTRY IS, before anything about who owns it. `--homes-root` is a caller
    # argument like any other and was never validated, so the three refusals this module
    # documents as "fail-closed in three independent places" could all be aimed somewhere
    # else with one flag: `--homes-root ~ --all --allow-unverifiable --delete` reported
    # `Documents … DELETED` and `Pictures … DELETED` and removed them (measured). The same
    # class and the same argument as the `--orchestration-id` separator hole fixed
    # earlier on this branch — operator argv, but missing input validation rather than
    # deliberate circumvention, and the loss is unrecoverable.
    #
    # The check is on the SHAPE rather than on the path, because the override exists for
    # tests and for an operator with a reason, and pinning it to the default root would
    # take the lever away. A backend home always has at least one subdirectory and every
    # subdirectory is a declared backend name — `_create_workflow_backend_home` makes the
    # backend directory before it writes the marker, so there is no valid state without
    # one. RESIDUE, stated: a half-deleted entry has neither and is refused here, and the
    # operator removes it by hand.
    subdirs = set(report["backends"])
    if not subdirs or not subdirs.issubset(set(WORKFLOW_BACKEND_HOME_DIRNAMES)):
        report["verdict"] = REFUSED_NOT_A_BACKEND_HOME
        return report
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


class _StatusChangedDuringPrune(Exception):
    """The owner's status stopped being terminal between the check and the delete."""

    def __init__(self, status: str) -> None:
        super().__init__(f"orchestration status changed to {status!r} before deletion")
        self.status = status


class _OwnerVanishedDuringPrune(Exception):
    """The owner's metadata disappeared between the check and the delete.

    A separate type from `_StatusChangedDuringPrune` because it routes to a DIFFERENT
    verdict, and the difference is what the operator is told to do: a status that turned
    non-terminal is cleared by terminalizing the run, while a checkout that is gone cannot
    be terminalized at all and wants `--allow-unverifiable` instead.
    """


def _delete_under_owner_lock(entry: Path, homes_root: Path,
                             report: dict[str, Any]) -> None:
    """Re-verify the status and delete, both inside the owner's metadata lock.

    `inspect_entry` reads the status under that lock and RELEASES it before returning,
    which left a window: a `--resume` resets a terminal orchestration to `running`, and
    the delete then went ahead on a verdict that was true a moment ago and destroyed the
    live leaf's session. Reproduced by resetting the status between the two calls.

    The lock is the right instrument because it is the one `update_orchestration_status`
    itself holds while writing a status, so a resume cannot slip between this re-read and
    the `rmtree` the way it could between the two calls.

    An entry going through the `--allow-unverifiable` route is skipped entirely: its
    verdict was never "the owner says this is terminal", so there is no verdict to
    re-verify and nothing to race. GATED ON THE VERDICT, not on whether a repo path
    happens to be recorded — an unverifiable entry usually HAS one (that is how the tool
    discovered the checkout is gone), and gating on the path alone sent it down the
    locked branch, where the absent metadata refused it. That turned the fix for the race
    into an over-refusal that made `--allow-unverifiable` useless, which is the direction
    fixes on this branch fail in most often; caught by the witness written for that flag.
    """
    raw_repo = report.get("owner_repo_root") or ""
    orchestration_id = report.get("orchestration_id") or ""
    if report.get("verdict") != VERDICT_DELETABLE or not raw_repo or not orchestration_id:
        _delete_entry(entry, homes_root)
        return
    repo_root = Path(raw_repo)
    with _orchestration_meta_exclusive_lock(repo_root, orchestration_id):
        status = _read_status_locked(repo_root, orchestration_id)
        if status is None:
            # The metadata that authorised this deletion is GONE since `inspect_entry`
            # read it — the checkout was removed mid-prune. It was verifiable a moment ago
            # and is not now, so the answer is the UNVERIFIABLE one, and `--allow-
            # unverifiable` is the operator's way of saying that is what they meant.
            #
            # It used to raise the NOT-TERMINAL refusal here, which was wrong in the way
            # that costs an operator time rather than data: that verdict is documented as
            # having no override and as being cleared by terminalizing the run — and
            # terminalizing needs the very metadata that just vanished, so the instruction
            # could not be followed. The comment above already argued for `unverifiable`
            # while the code did the other thing; a blank-slate reviewer read both.
            #
            # Reached only on the VERIFIED path: an entry unverifiable from the start
            # never takes the lock at all.
            raise _OwnerVanishedDuringPrune()
        if status not in DELETABLE_STATUSES:
            raise _StatusChangedDuringPrune(status)
        _delete_entry(entry, homes_root)


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
        except FileNotFoundError:
            # No root yet is an honest empty answer: no run has ever prepared a home.
            return [], 0
        except OSError as exc:
            # Present but unlistable (permissions, a broken mount) is NOT an empty root,
            # and reporting it as one told the operator "nothing to prune" about a tree
            # the tool could not see into.
            raise ValueError(f"cannot list the isolated homes root {homes_root}: {exc}") from exc
    reports: list[dict[str, Any]] = []
    refused_explicit = False
    for name in names:
        # THE NAME IS VALIDATED BEFORE IT BECOMES A PATH, for the same reason
        # `_workflow_backend_home_path` validates it on the writing side, and with the
        # same predicate so the two cannot drift. Without this, a `--orchestration-id`
        # carrying a path separator lands INSIDE the homes root — so the containment
        # assert in `_delete_entry` passes — while `inspect_entry` looks for
        # `owner.json` one level below where it lives and answers
        # `refused:unverifiable_owner`, which `--allow-unverifiable` releases.
        # `--orchestration-id orch-live/claude --allow-unverifiable --delete` therefore
        # deleted a RUNNING orchestration's home, defeating the one refusal this tool
        # documents as having no override. The names taken from `iterdir()` are single
        # components already; the check costs them nothing and covers both sources.
        if not _is_safe_path_id(name):
            reports.append({
                "orchestration_id": name,
                "path": "",
                "owner_repo_root": "",
                "status": "",
                "backends": [],
                "size_bytes": 0,
                "verdict": REFUSED_INVALID_ORCHESTRATION_ID,
                "deleted": False,
            })
            if name in explicit:
                refused_explicit = True
            continue
        report = inspect_entry(homes_root / name, name)
        deletable = _would_delete(report, allow_unverifiable=allow_unverifiable)
        report["deleted"] = False
        if deletable and delete:
            try:
                _delete_under_owner_lock(homes_root / name, homes_root, report)
            except _OwnerVanishedDuringPrune:
                report["verdict"] = REFUSED_UNVERIFIABLE_OWNER
                report["status"] = ""
                deletable = False
            except _StatusChangedDuringPrune as exc:
                report["verdict"] = REFUSED_NOT_TERMINAL
                report["status"] = exc.status
                deletable = False
            except (OSError, ValueError) as exc:
                report["verdict"] = f"refused:delete_failed:{exc}"
                # The exit code is decided from what HAPPENED, not from what was
                # allowed: an explicitly named entry whose delete then failed exited 0
                # while the docstring promises "0 = the requested work was done".
                deletable = False
            else:
                report["deleted"] = True
        if not deletable and name in explicit:
            refused_explicit = True
        reports.append(report)
    return reports, (2 if refused_explicit else 0)


def _would_delete(report: dict[str, Any], *, allow_unverifiable: bool) -> bool:
    """The ONE deletability rule, read by `prune` and by the report alike.

    It was spelled twice, and the two disagreed on exactly the case the flag exists for:
    the preview called an entry `refused:unverifiable_owner` while the same command plus
    `--delete` removed it. For a tool whose whole purpose is "decide before the
    irreversible thing", a preview that understates what will go is the one defect it
    must not have.
    """
    return report["verdict"] == VERDICT_DELETABLE or (
        allow_unverifiable and report["verdict"] == REFUSED_UNVERIFIABLE_OWNER)


def _render_text(reports: list[dict[str, Any]], *, delete: bool, homes_root: Path,
                 allow_unverifiable: bool = False) -> str:
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
        if report.get("deleted"):
            action = "DELETED"
        elif not delete and _would_delete(report, allow_unverifiable=allow_unverifiable):
            # The verdict is still printed beside it: an entry that will go only because
            # `--allow-unverifiable` was passed should say so, not merely say "would
            # delete" as though its owner had been checked.
            action = f"would delete ({report['verdict']})"
        else:
            action = report["verdict"]
        lines.append(
            f"{report['orchestration_id']}  status={report['status'] or '?'}  "
            f"backends={','.join(report['backends']) or '-'}  {size_s}  {action}")
        if report["owner_repo_root"]:
            lines.append(f"    owner: {report['owner_repo_root']}")
    if not delete:
        lines.append("")
        if any(_would_delete(r, allow_unverifiable=allow_unverifiable) for r in reports):
            lines.append("Report only. Re-run with --delete to remove the entries marked "
                         "'would delete'.")
        else:
            lines.append("Report only, and nothing here is deletable as invoked.")
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
    try:
        reports, code = prune(
        homes_root,
            orchestration_ids=list(args.orchestration_id) or None,
            delete=bool(args.delete),
            allow_unverifiable=bool(args.allow_unverifiable),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"homes_root": str(homes_root), "entries": reports}, indent=2))
    else:
        print(_render_text(reports, delete=bool(args.delete), homes_root=homes_root,
                           allow_unverifiable=bool(args.allow_unverifiable)))
    return code


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
