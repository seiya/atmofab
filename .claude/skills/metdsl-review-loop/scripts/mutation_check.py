#!/usr/bin/env python3
"""Revert each hunk of a change and check that a test actually fails.

A fix that no test would notice is a fix nobody can keep. This walks the hunks of a
commit range, reverts them one at a time in a throwaway worktree, runs the given test
command there, and reports the hunks that SURVIVE — the ones where the suite stayed
green without them.

Known blind spot: a hunk can be killed and the test still be worthless, if the test passes
for a different reason than its name claims. Hunk-level reverting cannot see that — pair
this with a mechanism-level deletion and with checking that each fixture has only one path
to the outcome it asserts. See the skill's "Before you hand it over (round 0)".

The checkout is never touched: every mutation happens in a `git worktree` under
`--workdir` (default `~/.cache/mutation-check`). Keeping it under $HOME matters for
met-dsl, where a few hook tests are sensitive to the checkout's filesystem depth.

    python3 mutation_check.py --range HEAD~1..HEAD --paths mcp_servers tools \\
        --test-cmd "python3 -m pytest tools/tests/test_build_runtime_server.py -q -x"

One test run per hunk is the whole cost, so the hunks are spread over `--jobs`
worktrees that run at the same time (default: min(cores - 2, 4)). Two more things cut the
wall clock, and both are the caller's to do: pass `-x` so a killed hunk stops at its
first failing test instead of finishing the suite, and keep `--test-cmd` narrowed to
the tests that could plausibly see the change. Measured on a 4-hunk met-dsl range with
an 805-test file: 5m52s serially without `-x`, 43s with both (21s of that the baseline).

A baseline run (nothing reverted) goes first. Without it a suite that is already red
reports every hunk as "killed" — a false green, and the failure mode of this script that
reads most like success.

The other one is a hunk `git apply -R` refuses (it overlaps a later change, so it cannot
be reverted alone). Nothing is tested for such a hunk, so it is reported as SKIPPED and
counted as a failure, never folded into "pinned".

Exit code is 1 when an UNANNOTATED hunk survives (a docstring-only survivor is
expected and does not fail the run), or when any hunk is inconclusive or skipped; 2 when
the baseline is red or the run cannot be trusted.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_HUNK_START = "@@"


def _is_test_file(path: str) -> bool:
    name = Path(path).name
    return "/tests/" in f"/{path}" or name.startswith("test_") or name.endswith("_test.py")


def _is_comment_only(patch: str) -> bool:
    """True when every line this hunk adds or removes is a `#` comment or blank.

    Such a hunk cannot change behaviour, so it is a GUARANTEED survivor — the same argument
    that excludes test files, and worth applying for the same reason: a survivor list whose
    entries are all expected is one nobody reads. Only `#` comments count; a docstring is a
    string literal and stays in the check, since a test may assert on one.
    """
    changed = [
        line[1:].strip()
        for line in patch.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    return bool(changed) and all(not text or text.startswith("#") for text in changed)


_SUITE_DID_NOT_RUN = (
    "errors during collection",
    "error during collection",
    "Interrupted: ",
    "no tests ran",
    "ERROR collecting",
)


def _suite_did_not_run(output: str) -> bool:
    """True when pytest exited nonzero WITHOUT observing the code under test.

    A reverted hunk can break collection — an import that no longer resolves, a fixture built at
    class-body scope, a syntax error — and pytest then exits nonzero having run nothing. Scored on
    the exit code alone that reads as `killed`, which is the worst possible false green: it says a
    test noticed the change when no test ran at all. A witness census on met-dsl PR #68 hit exactly
    this on three mutations; re-run with `--continue-on-collection-errors` they showed 41-47 real
    failures each, so the verdict was right by accident and would not have been on a fourth.
    """
    return any(marker in output for marker in _SUITE_DID_NOT_RUN)


def _docstring_only(repo: Path, head: str, path: str, patch: str) -> bool:
    """True when reverting this hunk changes only DOCSTRINGS of a Python module.

    Docstrings deliberately stay in the check (`_is_comment_only` excludes `#` comments only),
    because a test may assert on one — met-dsl pins prompt-template and contract text. But when
    nothing does, such a hunk is a guaranteed survivor, and an unlabelled guaranteed survivor is
    how a survivor list stops being read: measured on PR #67, 2 of 5 survivors were docstring
    edits reported exactly like the three real gaps beside them.

    So: still checked, still reported — labelled. Compared as ASTs with every docstring node
    emptied, which is exact rather than heuristic; a hunk that also moves code fails the compare
    and keeps its unqualified SURVIVED.
    """
    if not path.endswith(".py"):
        return False

    def _stripped(source: str) -> str | None:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body[0].value.value = ""
        return ast.dump(tree)

    try:
        current = _run(["git", "show", f"{head}:{path}"], cwd=repo)
    except RuntimeError:
        return False
    # Reverted in a scratch tree, never against the checkout: the caller's working tree is not
    # guaranteed to be at `head`, and this must not touch it in any case. `git apply` edits the
    # file in place (it has no `--stdout`), so the scratch copy is what gets read back.
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(current, encoding="utf-8")
        reverted = subprocess.run(
            ["git", "apply", "-R", "--recount", "-"],
            cwd=scratch, input=patch, text=True, capture_output=True)
        if reverted.returncode != 0:
            return False  # not provably prose; keep the unqualified SURVIVED
        after_source = target.read_text(encoding="utf-8")
    before, after = _stripped(current), _stripped(after_source)
    return before is not None and before == after


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
    return proc.stdout


#: A `VAR=value` prefix a shell applies to the command — only TMPDIR matters here.
_TMPDIR_ASSIGNMENT_RE = re.compile(r"(?:^|[;&|]\s*|\s)TMPDIR=")


def _split_hunks(diff_text: str) -> list[tuple[str, str]]:
    """(file, one-hunk patch) for every hunk, each patch applying on its own."""
    hunks: list[tuple[str, str]] = []
    header: list[str] = []
    current_file = ""
    body: list[str] = []

    def flush() -> None:
        if body:
            hunks.append((current_file, "".join(header) + "".join(body)))

    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            flush()
            body = []
            header = [line]
            current_file = line.split(" b/", 1)[-1].strip()
        elif not body and (
            line.startswith(("index ", "--- ", "+++ ", "old mode", "new mode",
                             "new file", "deleted file", "similarity", "rename "))
        ):
            header.append(line)
        elif line.startswith(_HUNK_START):
            flush()
            body = [line]
        elif body:
            body.append(line)
    flush()
    return hunks


def _run_tests(cmd: str, worktree: Path, tmpdir: Path,
               timeout: int) -> subprocess.CompletedProcess[str]:
    """Run the test command in `worktree`; only its exit code decides a verdict.

    Each worker gets its own TMPDIR: jobs run concurrently, and tests that write to a
    fixed name under the temp root would otherwise fight over it.
    """
    tmpdir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        cmd, cwd=worktree, shell=True, text=True, capture_output=True,
        timeout=timeout, env={**os.environ, "TMPDIR": str(tmpdir)})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--range", required=True,
                help="git range, two-dot or three-dot: HEAD~1..HEAD, origin/main...HEAD")
    ap.add_argument("--paths", nargs="*", default=[], help="restrict to these paths")
    ap.add_argument("--test-cmd", required=True,
                    help="run inside the worktree; only its exit code is read, so pass "
                         "-x (pytest) to stop at the first failure")
    ap.add_argument("--repo", default=".", help="repository (default: cwd)")
    ap.add_argument("--workdir", default=str(Path.home() / ".cache" / "mutation-check"))
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--jobs", type=int, default=0,
                    help="hunks to test at once, each in its own worktree "
                         "(default: min(cores - 2, 4); 1 = serial)")
    ap.add_argument("--skip-baseline", action="store_true",
                    help="do not verify the suite is green before mutating (the check "
                         "that stops an already-red suite from reporting every hunk killed)")
    ap.add_argument("--include-tests", action="store_true",
                    help="also revert hunks in test files (reverting a test can only "
                         "remove assertions, so these always survive — off by default)")
    ap.add_argument("--include-comments", action="store_true",
                    help="also revert hunks whose changed lines are all `#` comments "
                         "(they cannot change behaviour, so these always survive)")
    ap.add_argument("--keep", action="store_true", help="keep the worktrees for inspection")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    diff = _run(["git", "diff", args.range, "--", *args.paths], cwd=repo)
    hunks = _split_hunks(diff)
    if not args.include_tests:
        kept = [(f, p) for f, p in hunks if not _is_test_file(f)]
        if len(kept) != len(hunks):
            print(f"ignoring {len(hunks) - len(kept)} hunk(s) in test files "
                  f"(reverting a test only removes assertions); --include-tests to keep")
        hunks = kept
    if not args.include_comments:
        kept = [(f, p) for f, p in hunks if not _is_comment_only(p)]
        if len(kept) != len(hunks):
            print(f"ignoring {len(hunks) - len(kept)} comment-only hunk(s) "
                  f"(they cannot change behaviour, so they always survive); "
                  f"--include-comments to keep")
        hunks = kept
    if not hunks:
        print("no hunks in range — nothing to check")
        return 0

    jobs = args.jobs if args.jobs > 0 else max(1, min((os.cpu_count() or 3) - 2, 4))
    jobs = min(jobs, len(hunks))
    # `--test-cmd` runs through a shell, so a `TMPDIR=...` prefix in it OVERRIDES the per-job
    # TMPDIR set below and every job shares one temp root. That is the configuration met-dsl
    # has already been bitten by: two suite runs on one TMPDIR produce failures that belong to
    # neither, and here such a failure is recorded as `killed` — a hunk reported pinned that
    # nothing pins. The canonical suite command in the skill carries exactly that prefix, so
    # this is ordinary usage, not a corner: refuse it rather than silently mismeasure.
    if _TMPDIR_ASSIGNMENT_RE.search(args.test_cmd):
        if jobs > 1:
            print("--test-cmd sets TMPDIR, which overrides the per-job temp root and makes "
                  f"{jobs} jobs share one. Drop the TMPDIR= prefix (this script sets it per "
                  "job, under $TMPDIR), or pass --jobs 1 to accept one shared root.")
            return 2
        print("note: --test-cmd sets TMPDIR, overriding the per-job temp root. Harmless at "
              "--jobs 1.")
    if "pytest" in args.test_cmd and not any(
            flag in args.test_cmd.split() for flag in ("-x", "--exitfirst")):
        print("hint: only the exit code is read — adding -x to --test-cmd ends each "
              "killed hunk at its first failing test")

    # `rsplit`, and strip the dot a THREE-dot range leaves behind: `origin/main...HEAD` is the
    # spelling the skill names as the review target, and `split("..")[-1]` turns it into `.HEAD`,
    # which `rev-parse` refuses. Resolved here, before any worktree or temp dir exists, so a bad
    # range fails with nothing to clean up (it used to raise between the mkdtemps and the `try`
    # that owns cleanup, leaving both behind).
    head = _run(["git", "rev-parse", args.range.rsplit("..", 1)[-1].lstrip(".") or "HEAD"],
                cwd=repo).strip()

    Path(args.workdir).mkdir(parents=True, exist_ok=True)
    # Siblings, not `<root>/wt0` — a nested layout makes every checkout path a few
    # characters longer, and met-dsl has at least one test whose budget the checkout
    # path feeds into and which sits 1 character from its limit. Each worktree keeps
    # exactly the path shape a single-job run has always had.
    worktrees = [Path(tempfile.mkdtemp(prefix="mut-", dir=args.workdir))
                 for _ in range(jobs)]
    # One TMPDIR per job, since concurrent jobs would otherwise share the temp root.
    # Named as short as `mkdtemp` allows: a met-dsl test asserts a budget on a message
    # that carries a temp path, and a long temp root alone can fail it.
    tmp_base = os.environ.get("TMPDIR", "/dev/shm")
    tmpdirs = {wt: Path(tempfile.mkdtemp(prefix="m", dir=tmp_base)) for wt in worktrees}
    for wt in worktrees:
        _run(["git", "worktree", "add", "--detach", str(wt), head], cwd=repo)

    free: list[Path] = list(worktrees)
    lock = threading.Lock()
    results: dict[int, tuple[str, str, str]] = {}
    started = time.monotonic()

    def check(index: int, path: str, patch: str) -> None:
        """Revert one hunk in a free worktree and record killed / SURVIVED / SKIP."""
        first = next((ln for ln in patch.splitlines() if ln.startswith(_HUNK_START)), "")
        with lock:
            wt = free.pop()
        try:
            _run(["git", "checkout", "--", "."], cwd=wt, check=False)
            revert = subprocess.run(
                ["git", "apply", "-R", "--recount", "-"], cwd=wt,
                input=patch, text=True, capture_output=True)
            if revert.returncode != 0:
                verdict = "SKIP (cannot revert in isolation)"
            else:
                proc = _run_tests(args.test_cmd, wt, tmpdirs[wt], args.timeout)
                if not proc.returncode:
                    verdict = "SURVIVED — no test noticed this hunk"
                elif _suite_did_not_run(proc.stdout + proc.stderr):
                    verdict = ("INCONCLUSIVE — the suite did not run (collection error / no "
                               "tests): nonzero exit, but nothing observed this hunk")
                else:
                    verdict = "killed"
        finally:
            with lock:
                free.append(wt)
        with lock:
            results[index] = (path, first, verdict)
            print(f"[{len(results)}/{len(hunks)} done] {path} {first[:60]}\n"
                  f"    {verdict}", flush=True)

    survivors: list[tuple[int, str, str]] = []
    inconclusive: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    baseline_failed = False
    try:
        print(f"{len(hunks)} hunk(s); {jobs} job(s) in {args.workdir}\n")
        if not args.skip_baseline:
            base = _run_tests(args.test_cmd, worktrees[0], tmpdirs[worktrees[0]],
                              args.timeout)
            if base.returncode:
                print("BASELINE RED — the suite fails with nothing reverted, so every "
                      "hunk would report 'killed' for a reason that is not the hunk.\n"
                      "Fix the suite (or narrow --test-cmd) and rerun. A suite that is "
                      "green in the checkout but red here is usually reading its own "
                      "path: the worktree and TMPDIR differ. Last lines:\n")
                print("\n".join((base.stdout + base.stderr).splitlines()[-25:]))
                baseline_failed = True
            else:
                print(f"baseline green in {time.monotonic() - started:.0f}s\n")
        if not baseline_failed:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                for future in [pool.submit(check, i, f, p)
                               for i, (f, p) in enumerate(hunks, 1)]:
                    future.result()
            survivors = [(i, path, first) for i, (path, first, verdict) in
                         sorted(results.items()) if verdict.startswith("SURVIVED")]
            inconclusive = [(path, first) for _i, (path, first, verdict) in
                            sorted(results.items()) if verdict.startswith("INCONCLUSIVE")]
            skipped = [(path, first) for _i, (path, first, verdict) in
                       sorted(results.items()) if verdict.startswith("SKIP")]
            prose = {i for i, path, _first in survivors
                     if _docstring_only(repo, head, path, hunks[i - 1][1])}
    finally:
        for tmp in tmpdirs.values():
            shutil.rmtree(tmp, ignore_errors=True)
        if not args.keep:
            for wt in worktrees:
                shutil.rmtree(wt, ignore_errors=True)
            _run(["git", "worktree", "prune"], cwd=repo, check=False)
        else:
            print("\nworktrees kept at:\n  " + "\n  ".join(str(w) for w in worktrees))

    if baseline_failed:
        return 2
    print(f"\n{len(hunks)} hunk(s) in {time.monotonic() - started:.0f}s")
    if skipped:
        print(f"{len(skipped)} SKIPPED hunk(s) — `git apply -R` refused them, so NOTHING was "
              f"tested for these. Not a pass: revert them by hand, or narrow --range so they "
              f"apply in isolation:")
        for path, first in skipped:
            print(f"  {path} {first[:70]}")
    if inconclusive:
        print(f"{len(inconclusive)} INCONCLUSIVE hunk(s) — the suite exited nonzero without "
              f"running, so nothing observed them. Add `--continue-on-collection-errors` to "
              f"--test-cmd and rerun; do NOT read these as killed:")
        for path, first in inconclusive:
            print(f"  {path} {first[:70]}")
    if survivors:
        real = [s for s in survivors if s[0] not in prose]
        print(f"{len(survivors)} surviving hunk(s)"
              + (f", {len(prose)} of them docstring-only:" if prose else ":"))
        for i, path, first in survivors:
            tag = "  [docstring-only — expected]" if i in prose else ""
            print(f"  {path} {first[:70]}{tag}")
        if prose:
            print("\nA docstring-only hunk changes no behaviour, so it ALWAYS survives. It is "
                  "still checked (a test may pin prose) and it is labelled so it does not read "
                  "as a finding: on met-dsl PR #67, 2 of 5 survivors were docstrings printed "
                  "identically to the three real gaps beside them.")
        if real:
            print(f"\n{len(real)} unexplained survivor(s). Either the behavior has no pin, or "
                  "an existing test kills it for a different reason. Both are worth knowing "
                  "before committing.\nOne expected survivor: half of a code MOTION. Reverting "
                  "the deletion while the moved copy remains changes nothing — read the pair "
                  "together.")
        return 1 if (real or inconclusive or skipped) else 0
    if inconclusive or skipped:
        return 1
    print("every hunk is pinned")
    print("NOT the same as 'the tests are adequate'. Reverting a hunk cannot detect a test\n"
          "that passes for a DIFFERENT reason than its name claims: the hunk is live, just\n"
          "unobserved. In met-dsl L128 the whole scope analysis could be replaced by a\n"
          "pass-through and the suite stayed green, while every individual hunk was 'pinned'.\n"
          "Also run: one MECHANISM-level deletion (stub the function out), and check that each\n"
          "fixture has no second path to the outcome it asserts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
