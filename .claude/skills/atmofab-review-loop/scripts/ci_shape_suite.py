#!/usr/bin/env python3
"""Run the suite as CI sees it, then the skip-report check, in one command.

CI has none of the executables `tools/tests/skip_probes.py` `DEPENDENCIES` names (the CUDA
compiler driver and the two leaf CLIs), and none of the operator's home: no credentials file,
no CLI configuration. A run on the operator's host has both, so a row that reads either is
green here and red in CI, and a row added behind a host tool with a bare `skipUnless` is a skip
only CI refuses (issue #589 PR-1b: 37 rows; PR #557: 5 rows binding the credentials file).

What this script does, so it is not rebuilt by hand each time:

* HOME is a fresh empty directory.
* PATH keeps every directory that holds none of the hidden executables. A directory that holds
  one is replaced by a shim directory of symlinks to everything else in it — dropping the whole
  directory also drops the linters installed beside a CLI (measured: 129 and 220 false failures
  on two branches that did that).
* PYTHONPATH carries the user site-packages: under an empty HOME the interpreter no longer
  finds packages installed with `pip install --user`, pytest among them, and the run then dies
  with `No module named pytest` and prints no summary line.
* The suite runs under `--junitxml`, and `python3 -m tools.tests.skip_probes` reads that report
  in the SAME environment, which is the host the check must run on (its docstring says why).

Run from the checkout root:

    python3 .claude/skills/atmofab-review-loop/scripts/ci_shape_suite.py [-- <extra pytest args>]

`-n <workers>` is added when pytest-xdist is importable (pass `--jobs` to choose; default 24).
The temporary directory is removed at exit unless `--keep` is given.
Exit status: 0 only when the suite passed AND the skip check refused nothing.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import site
import subprocess
import sys
import tempfile
from pathlib import Path


def hidden_executables() -> set[str]:
    """The executable names CI lacks, read from the one definition (`skip_probes.DEPENDENCIES`)."""
    sys.path.insert(0, os.getcwd())
    from tools.tests import skip_probes

    return {thunk() for thunk, _reason in skip_probes.DEPENDENCIES.values()}


def ci_path(path: str, hide: set[str], shim: Path) -> str:
    """PATH with every executable in `hide` absent and every other one still reachable.

    A directory holding none of `hide` is kept as it is. A directory holding one of them is
    dropped, and each OTHER executable in it is symlinked into `shim`, which goes first; the
    first directory to provide a name wins, as on the original PATH.
    """
    kept: list[str] = []
    for entry in path.split(os.pathsep):
        if not entry or not os.path.isdir(entry):
            continue
        if not any(os.path.exists(os.path.join(entry, name)) for name in hide):
            kept.append(entry)
            continue
        for name in sorted(os.listdir(entry)):
            src = os.path.join(entry, name)
            dst = shim / name
            if name in hide or os.path.lexists(dst):
                continue
            if os.path.isfile(src) and os.access(src, os.X_OK):
                dst.symlink_to(src)
    return os.pathsep.join([str(shim), *kept])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=24, help="pytest-xdist workers (default 24)")
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary HOME, shim and report (removed by default)")
    parser.add_argument("pytest_args", nargs="*", help="extra pytest arguments, after --")
    args = parser.parse_args(argv)

    if not Path("tools/tests").is_dir():
        print("run from the checkout root (tools/tests not found)", file=sys.stderr)
        return 2
    hide = hidden_executables()
    root = Path(tempfile.mkdtemp(prefix="ci-shape-"))
    try:
        return _run(args, hide, root)
    finally:
        if args.keep:
            print(f"kept {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


def _run(args: argparse.Namespace, hide: set[str], root: Path) -> int:
    shim, home, report = root / "shim", root / "home", root / "report.xml"
    shim.mkdir()
    home.mkdir()
    env = dict(os.environ, HOME=str(home), PATH=ci_path(os.environ.get("PATH", ""), hide, shim))
    user_site = site.getusersitepackages()
    env["PYTHONPATH"] = os.pathsep.join(p for p in (user_site, os.environ.get("PYTHONPATH")) if p)

    for name in sorted(hide):
        found = subprocess.run(["sh", "-c", f"command -v {name}"], env=env, capture_output=True, check=False)
        if found.returncode == 0:
            print(f"{name} is still on the CI-shaped PATH", file=sys.stderr)
            return 2
    print(f"hidden: {', '.join(sorted(hide))}; HOME={home}; report={report}")

    cmd = [sys.executable, "-m", "pytest", "tools/tests", "-q", "-p", "no:randomly", "-rs",
           f"--junitxml={report}"]
    if importlib.util.find_spec("xdist") is not None:
        cmd += ["-n", str(args.jobs)]
    cmd += args.pytest_args
    suite = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    lines = [line for line in suite.stdout.splitlines() if line.strip()]
    print(lines[-1] if lines else "(pytest printed nothing)")
    if suite.returncode != 0:
        print("\n".join(line for line in lines if line.startswith(("FAILED", "ERROR"))))
        print(suite.stderr[-2000:], file=sys.stderr)
    if not report.is_file():
        print("no JUnit report was written: the skip check did not run", file=sys.stderr)
        return 1
    check = subprocess.run([sys.executable, "-m", "tools.tests.skip_probes", str(report)],
                           env=env, capture_output=True, text=True, check=False)
    summary = [line for line in check.stdout.splitlines() if line.startswith("skipped=")]
    print(summary[-1] if summary else check.stdout[-2000:])
    if check.returncode != 0:
        print(check.stdout[-4000:])
    return 0 if suite.returncode == 0 and check.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
