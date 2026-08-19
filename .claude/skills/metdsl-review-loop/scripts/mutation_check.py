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

The other one is a hunk nothing can be tested for: one carrying a rename (reverting it
reverses the rename, so the suite answers about the file's absence), or one `git apply -R`
refuses. Those are reported as SKIPPED and counted as a failure, never folded into "pinned".
A change with no revertible hunk at all — a pure rename, a binary file, a mode change, an
empty new file — is listed by name and fails the run the same way, even when every hunk
beside it is pinned. (A symlink whose target changes is NOT such a case: it produces an
ordinary hunk and is scored normally.)

Exit code is 1 when an UNANNOTATED hunk survives (a docstring-only survivor is
expected and does not fail the run), or when any hunk is inconclusive or skipped; 2 when
the baseline is red, or when `--test-cmd` sets TMPDIR while several jobs run, which would
put them on one temp root, or the BASELINE hits `--timeout`.

A range with no hunks left to check — a wrong base that still resolves, `--paths` that
matches nothing, or everything filtered out as a test file — prints that and
exits 0. It is not a pass: nothing was tested. Read the hunk count the run prints, never the
exit code alone. A base that does not resolve at all, or a `--repo` that is not one, exits 2.

`--timeout` (default 1800s) applies to each test run; hitting it makes that hunk
INCONCLUSIVE rather than killing the process.
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


#: Suffixes whose comments this script can recognise EXACTLY, by comparing parsed code rather
#: than by looking at the line. `#` is not a reliable marker on its own: it opens a heading in
#: Markdown (this repository pins `##` sections out of committed documents), a preprocessor
#: directive in the c/cpp families, a shebang in a shell script, a lint pragma such as
#: `noqa` or `type: ignore` — and inside a Python string literal or a YAML block scalar it is just text, which is
#: exactly the prompt-template and contract text met-dsl pins. Two review rounds broke a
#: line-shaped predicate in two different shapes, so the predicate is not line-shaped any more:
#: only Python is classified, and only through its AST.
_PARSEABLE_SUFFIXES = frozenset({".py", ".pyi"})


_SUITE_DID_NOT_RUN = (
    "errors during collection",
    "error during collection",
    "Interrupted: ",
    "no tests ran",
    "ERROR collecting",
)


#: A run that reports failures observed something, whatever else went wrong beside it. Matched
#: against the TAIL of the output only: pytest puts its counts in the last lines, while the body
#: carries whatever the tests printed — an assertion message, a captured subprocess log — and a
#: `3 failed,` anywhere in that body used to outrank a collection marker and score the hunk
#: `killed` although nothing had run.
_TESTS_DID_FAIL_RE = re.compile(r"\b\d+ failed\b")
_SUMMARY_TAIL_LINES = 6


def _suite_did_not_run(output: str) -> bool:
    """True when the test command exited nonzero WITHOUT observing the code under test.

    A reverted hunk can break collection — an import that no longer resolves, a fixture built at
    class-body scope, a syntax error — and pytest then exits nonzero having run nothing. Scored on
    the exit code alone that reads as `killed`, which is the worst possible false green: it says a
    test noticed the change when no test ran at all. A witness census on met-dsl PR #68 hit exactly
    this on three mutations; re-run with `--continue-on-collection-errors` they showed 41-47 real
    failures each, so the verdict was right by accident and would not have been on a fourth.

    A collection marker alone is not the question, though, and reading it as one made the remedy
    this script prints useless: with `--continue-on-collection-errors` pytest keeps the same
    `ERROR collecting` line AND runs the rest, so a hunk its tests really killed came back
    INCONCLUSIVE however many times the reader followed the advice. Reported failures settle it —
    something ran and noticed — so they win over the marker.

    Two limits, both measured. The counts are read from the TAIL of the output, because a test
    that prints `3 failed,` (a captured log, an assertion message) would otherwise decide the
    verdict from the body. And `-x` cancels `--continue-on-collection-errors`: pytest stops at
    the first error, so a collection error can end the run with nothing else attempted, and the
    remedy this prints only works if the reader drops `-x` as well.
    """
    tail = "\n".join(output.strip().splitlines()[-_SUMMARY_TAIL_LINES:])
    if _TESTS_DID_FAIL_RE.search(tail):
        return False
    return any(marker in output for marker in _SUITE_DID_NOT_RUN)


def _docstring_only(repo: Path, head: str, path: str, patch: str) -> bool:
    """True when reverting this hunk changes only PROSE of a Python module: comments, docstrings.

    Prose stays in the check rather than being filtered out, because a test may assert on it —
    met-dsl pins prompt-template and contract text. But when nothing does, such a hunk is a
    guaranteed survivor, and an unlabelled guaranteed survivor is how a survivor list stops being
    read: measured on PR #67, 2 of 5 survivors were docstring edits reported exactly like the
    three real gaps beside them.

    So: still checked, still reported — labelled. Compared as ASTs with every docstring node
    emptied, which is exact rather than heuristic: comments are absent from an AST, so a
    comment-only edit compares equal, while a `#` line added INSIDE a string literal changes a
    Constant and does not. A hunk that also moves code fails the compare and keeps its
    unqualified SURVIVED.
    """
    if Path(path).suffix not in _PARSEABLE_SUFFIXES:
        return False

    def _stripped(source: str) -> str | None:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        except ValueError:
            # A non-UTF-8 file arrives here carrying surrogates from `surrogateescape`, and
            # `compile()` refuses those with UnicodeEncodeError — a ValueError subclass, which
            # is why this arm is spelled at that width — not SyntaxError. Uncaught it
            # was a traceback and exit 1 — the code that means "hunks survived" — AFTER the
            # verdicts had been printed. Unclassifiable is the honest answer: not prose.
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
        return False  # the file is not at head (the change deleted it): not classifiable
    # Reverted in a scratch tree, never against the checkout: the caller's working tree is not
    # guaranteed to be at `head`, and this must not touch it in any case. `git apply` edits the
    # file in place (it has no `--stdout`), so the scratch copy is what gets read back.
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(current.encode("utf-8", "surrogateescape"))
        reverted = subprocess.run(
            ["git", "apply", "-R", "--recount", "-"], cwd=scratch,
            input=patch.encode("utf-8", "surrogateescape"), capture_output=True)
        if reverted.returncode != 0:
            return False  # not provably prose; keep the unqualified SURVIVED
        try:
            after_source = target.read_bytes().decode("utf-8", "surrogateescape")
        except OSError:
            # Nothing is at `path` after the revert. The reachable case is a hunk that ADDS a
            # file: reverting it deletes the file. (A rename would do the same, but those are
            # SKIPped in `check()` before any classification, so that case cannot arrive here —
            # an earlier version of this comment named it as the reason.) Either way it is not
            # a prose change, and without this the OSError escaped as an exit-2 `cannot run:`
            # after the verdicts had already printed.
            return False
    before, after = _stripped(current), _stripped(after_source)
    # `is not None` on BOTH sides. The left one has always been there; the right one is
    # defensive, not a fix — with `_stripped` returning None for an unparseable or non-UTF-8
    # file, the equality alone was already False. It is spelled out because the failure it
    # guards against is silent: any future `_stripped` that returns a falsy value instead of
    # None would make both sides compare equal and label a real change as expected prose.
    return before is not None and after is not None and before == after


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> str:
    """Run git and return its stdout with line endings INTACT.

    Bytes, not `text=True`: text mode translates `\r\n` to `\n`, and `git apply -R` then
    refuses the patch it is handed. Measured before this: every hunk of a CRLF file came back
    `SKIP (cannot revert in isolation)` while the same patch written to a file applied fine.
    """
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True)
    if check and proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"{' '.join(cmd)} failed: "
                           f"{detail[0] if detail else '(no message)'}")
    # `surrogateescape`, not `replace`: a latin-1 source file must survive the round trip back
    # into `git apply`, which otherwise refuses the patch and every hunk of that file SKIPs.
    return proc.stdout.decode("utf-8", "surrogateescape")


#: A `VAR=value` assignment a shell applies to the command — only TMPDIR matters here.
#: Anything that is not a name character before `TMPDIR=` starts an assignment, which covers
#: the quoted and sub-shell forms (`sh -c "TMPDIR=… …"`, `(TMPDIR=… …)`) that an earlier
#: version — anchored on whitespace and `;&|` — read straight past. `=`, `/`, `:`, `-` and `.`
#: before it mean it is part of a path, a flag value or a test id, not an assignment.
_TMPDIR_ASSIGNMENT_RE = re.compile(r"(?<![\w=/:.\-])TMPDIR=")

#: `git diff` marks a rename with these; a hunk carrying one cannot be reverted alone.
_RENAME_HEADER_RE = re.compile(r"^rename (from|to) ", re.MULTILINE)


def _diff_entry_path(header_line: str) -> str:
    """The post-image path of a `diff --git` line, for the paths git actually produces.

    Splitting on `" b/"` — the obvious reading — takes the LAST occurrence, so a file under a
    directory named `pkg b` came out as `lib.py b/pkg b/lib.py`: the test-file filter, the
    prose annotation and the printed name were all wrong for it, and the annotation failure
    surfaced as a false "unexplained survivor". A path with a space, a quote or a non-ASCII
    byte is quoted by git (`diff --git "a/…" "b/…"`), where `" b/"` does not appear at all.
    """
    rest = header_line[len("diff --git "):].strip()
    if rest.startswith('"'):
        # Two C-quoted paths; the post-image is the second. Escapes are git's own (\t, \337).
        parts = re.findall(r'"((?:[^"\\]|\\.)*)"', rest)
        if len(parts) == 2:
            raw = (parts[1].encode("latin-1", "backslashreplace")
                   .decode("unicode_escape").encode("latin-1")
                   .decode("utf-8", "surrogateescape"))
            return raw[2:] if raw.startswith("b/") else raw
    half = len(rest) // 2
    if rest[half:half + 1] == " " and rest[:half].startswith("a/") and rest[half + 1:].startswith("b/"):
        # Equal-length halves is what git emits for an ordinary path: `a/<p> b/<p>`.
        return rest[half + 3:]
    return rest.split(" b/", 1)[-1]


def _hunkless_files(diff_text: str) -> list[str]:
    """Files the diff names but gives no `@@` body for: pure renames, binaries, mode changes.

    They cannot be hunk-reverted, and before they were counted they fell into the same
    `no hunks in range — nothing to check` line as a wrong base, so a rename-only or
    binary-only round exited 0 having tested nothing.
    """
    named: list[str] = []
    current = ""
    saw_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current and not saw_hunk:
                named.append(current)
            current = _diff_entry_path(line)
            saw_hunk = False
        elif line.startswith(_HUNK_START):
            saw_hunk = True
    if current and not saw_hunk:
        named.append(current)
    return named


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
            current_file = _diff_entry_path(line)
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
               timeout: int) -> subprocess.CompletedProcess[str] | None:
    """Run the test command in `worktree`; its exit code decides the verdict.

    Each worker gets its own TMPDIR: jobs run concurrently, and tests that write to a
    fixed name under the temp root would otherwise fight over it.
    """
    try:
        return subprocess.run(
            cmd, cwd=worktree, shell=True, text=True, capture_output=True,
            timeout=timeout, env={**os.environ, "TMPDIR": str(tmpdir)})
    except subprocess.TimeoutExpired:
        # Nothing observed the hunk, so this is INCONCLUSIVE, not a kill. Letting the
        # exception escape printed a traceback and exited 1 with no summary, which a caller
        # reading the exit code cannot tell from "an unannotated hunk survived".
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--range", required=True,
                help="git range, two-dot or three-dot: HEAD~1..HEAD, origin/main...HEAD")
    ap.add_argument("--paths", nargs="*", default=[], help="restrict to these paths")
    ap.add_argument("--test-cmd", required=True,
                    help="run inside the worktree; its exit code decides the verdict and its "
                         "output is read only to tell a real failure from a suite that never "
                         "ran, so pass -x (pytest) to stop at the first failure")
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
    ap.add_argument("--keep", action="store_true", help="keep the worktrees for inspection")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    # `-c diff.noprefix=false` because with the caller's `diff.noprefix=true` the `--- `
    # header stops matching and EVERY hunk skips — measured. `--no-ext-diff`/`--no-color` are
    # belt and braces for the same class (a configured external differ, a forced color): git
    # emits no color through a pipe, so neither is witnessed by a scenario I could build.
    diff = _run(["git", "-c", "diff.noprefix=false", "diff", "--no-ext-diff", "--no-color",
                 args.range, "--", *args.paths], cwd=repo)
    hunks = _split_hunks(diff)
    hunkless = _hunkless_files(diff)
    if not args.include_tests:
        kept = [(f, p) for f, p in hunks if not _is_test_file(f)]
        if len(kept) != len(hunks):
            print(f"ignoring {len(hunks) - len(kept)} hunk(s) in test files "
                  f"(reverting a test only removes assertions); --include-tests to keep")
        hunks = kept
    if hunkless:
        print(f"{len(hunkless)} change(s) with no revertible hunk — a pure rename, a binary "
              f"file, a mode change, an empty new file. NOTHING was tested for these:")
        for name in hunkless:
            print(f"  {name}")
    if not hunks:
        print("no hunks in range — nothing to check"
              + (" beyond the change(s) above" if hunkless else ""))
        return 1 if hunkless else 0

    jobs = args.jobs if args.jobs > 0 else max(1, min((os.cpu_count() or 3) - 2, 4))
    jobs = min(jobs, len(hunks))
    # `--test-cmd` runs through a shell, so a `TMPDIR=...` prefix in it OVERRIDES the per-job
    # TMPDIR set below and every job shares one temp root. That is the configuration met-dsl
    # has already been bitten by: two suite runs on one TMPDIR produce failures that belong to
    # neither, and here such a failure is recorded as `killed` — a hunk reported pinned that
    # nothing pins. It is the spelling this repository's own suite command has always carried
    # (the skill now tells you to drop it HERE, for this reason), so it is ordinary usage, not
    # a corner: refuse it rather than silently mismeasure.
    if _TMPDIR_ASSIGNMENT_RE.search(args.test_cmd):
        if jobs > 1:
            print("--test-cmd sets TMPDIR, which overrides the per-job temp root and makes "
                  f"{jobs} jobs share one. Drop the TMPDIR= prefix (this script sets it per "
                  "job, under $TMPDIR), or pass --jobs 1 to accept one shared root.")
            return 2
        print("note: --test-cmd sets TMPDIR, overriding the per-job temp root. Harmless at "
              "--jobs 1.")
    if "pytest" in args.test_cmd and not re.search(r"(?<![\w-])(-x|--exitfirst)(?![\w-])",
                                                   args.test_cmd):
        print("hint: the exit code decides the verdict — adding -x to --test-cmd ends each "
              "killed hunk at its first failing test")

    # `rsplit`, not `split`: `origin/main...HEAD` is the spelling the skill names as the review
    # target, and `split("..")[-1]` turns it into `.HEAD`, which `rev-parse` refuses. `rsplit`
    # on the two-dot separator leaves the third dot on the LEFT half, so nothing needs stripping
    # (an earlier version added a `.lstrip(".")` that a census proved could never fire). Resolved here, before any worktree or temp dir exists, so a bad
    # range fails with nothing to clean up (it used to raise between the mkdtemps and the `try`
    # that owns cleanup, leaving both behind).
    head = _run(["git", "rev-parse", args.range.rsplit("..", 1)[-1] or "HEAD"],
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
            # `git checkout -- .` restores tracked files ONLY. A hunk whose revert CREATES a
            # file — the change deleted or renamed one — leaves that file untracked in this
            # worktree, and the next hunk scheduled here inherits it: its suite fails for the
            # leftover and the hunk is scored `killed`. Measured: a two-commit range (one
            # deletion, one unpinned addition) reported "every hunk is pinned" and exit 0 at
            # --jobs 1, while --jobs 2 correctly reported the addition as SURVIVED — the
            # verdict depended on how many jobs the machine chose. `-x` as well as `-fd`,
            # because a stale ignored artifact (a `__pycache__`, a leftover workspace dir) is
            # the same class of carry-over.
            _run(["git", "checkout", "--", "."], cwd=wt, check=False)
            _run(["git", "clean", "-qfdx"], cwd=wt, check=False)
            if _RENAME_HEADER_RE.search(patch):
                # `git apply -R` reverses the rename along with the hunk, so the suite is
                # answering about the file's absence at its new path, not about this hunk.
                # Measured: a docstring-only edit inside a renamed module came back `killed`
                # when the failing import sat in a test body and INCONCLUSIVE when it sat at
                # module scope — a verdict decided by where someone wrote an import.
                verdict = ("SKIP (carries a rename — reverting it reverses the rename, so no "
                           "verdict here is about the hunk; check the content separately)")
            else:
                revert = subprocess.run(
                    ["git", "apply", "-R", "--recount", "-"], cwd=wt,
                    input=patch.encode("utf-8", "surrogateescape"), capture_output=True)
                if revert.returncode != 0:
                    verdict = "SKIP (cannot revert in isolation)"
                else:
                    proc = _run_tests(args.test_cmd, wt, tmpdirs[wt], args.timeout)
                    if proc is None:
                        verdict = ("INCONCLUSIVE — the test command hit --timeout, so nothing "
                                   "observed this hunk")
                    elif not proc.returncode:
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
        # Inside the block that owns cleanup, not before it: a failure part-way through this
        # loop used to leave the earlier worktrees REGISTERED in the caller's repository
        # (`git worktree list` showed them) with only a tidy "cannot run:" line to go on.
        for wt in worktrees:
            _run(["git", "worktree", "add", "--detach", str(wt), head], cwd=repo)
        print(f"{len(hunks)} hunk(s); {jobs} job(s) in {args.workdir}\n")
        if not args.skip_baseline:
            base = _run_tests(args.test_cmd, worktrees[0], tmpdirs[worktrees[0]],
                              args.timeout)
            if base is None:
                print(f"BASELINE TIMED OUT after {args.timeout}s with nothing reverted. Every "
                      f"hunk would time out the same way, so nothing could be measured. Raise "
                      f"--timeout or narrow --test-cmd.")
                baseline_failed = True
            elif base.returncode:
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
        print(f"{len(skipped)} SKIPPED hunk(s) — NOTHING was tested for these, for the reason "
              f"each line gives. Not a pass: revert them by hand, or narrow --range so the "
              f"content can be reverted without its rename:")
        for path, first in skipped:
            print(f"  {path} {first[:70]}")
    if inconclusive:
        print(f"{len(inconclusive)} INCONCLUSIVE hunk(s) — nothing observed them, for the reason "
              f"each line gives. A collection error wants `--continue-on-collection-errors` on "
              f"--test-cmd AND `-x` removed from it (they cancel: -x stops at the first error); "
              f"a timeout wants a longer --timeout or a narrower --test-cmd. Do NOT read these "
              f"as killed:")
        for path, first in inconclusive:
            print(f"  {path} {first[:70]}")
    if survivors:
        real = [s for s in survivors if s[0] not in prose]
        print(f"{len(survivors)} surviving hunk(s)"
              + (f", {len(prose)} of them prose-only:" if prose else ":"))
        for i, path, first in survivors:
            tag = "  [prose-only (comment/docstring) — expected]" if i in prose else ""
            print(f"  {path} {first[:70]}{tag}")
        if hunkless:
            print(f"\nAnd {len(hunkless)} change(s) listed at the top had no revertible hunk, "
                  f"so nothing above is a verdict about them.")
        if prose:
            print("\nA prose-only hunk — comments, docstrings — changes no behaviour, so it "
                  "ALWAYS survives. It is still checked (a test may pin prose, and this repo "
                  "does) and it is labelled so it does not read as a finding: on met-dsl PR #67, "
                  "2 of 5 survivors were docstrings printed identically to the three real gaps "
                  "beside them. Only Python is classified this way, by comparing ASTs; in any "
                  "other file type a prose hunk is reported unlabelled.")
        if real:
            print(f"\n{len(real)} unexplained survivor(s). Either the behavior has no pin, or "
                  "an existing test kills it for a different reason. Both are worth knowing "
                  "before committing.\nOne expected survivor: half of a code MOTION. Reverting "
                  "the deletion while the moved copy remains changes nothing — read the pair "
                  "together.")
        return 1 if (real or inconclusive or skipped or hunkless) else 0
    if inconclusive or skipped or hunkless:
        if hunkless and not (inconclusive or skipped):
            print(f"\nEvery hunk is pinned, but {len(hunkless)} change(s) listed at the top had "
                  f"no revertible hunk and were never tested — this is not a clean run.")
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
    try:
        sys.exit(main())
    except (RuntimeError, OSError) as exc:
        # A git invocation this script depends on failed: a range whose base does not resolve
        # (a stale `origin/main` is the common one), a `--repo` that is not a repository or
        # does not exist (that one surfaces as OSError from the spawn itself, not from git).
        # That is the instrument failing, not a finding, so it exits 2 like a red baseline —
        # exit 1 is documented as "hunks survived" and a traceback there reads as findings.
        print(f"cannot run: {exc}", file=sys.stderr)
        sys.exit(2)
