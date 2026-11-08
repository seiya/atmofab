#!/usr/bin/env python3
"""Run the whole suite without the dependencies CI lacks, then the skip-report check, in one command.

CI has none of the executables `tools/tests/skip_probes.py` `DEPENDENCIES` names (the CUDA
compiler driver and the two leaf CLIs), and none of the operator's home: no credentials file,
no CLI configuration. A run on the operator's host has both, so a row that reads either is
green here and red in CI, and a row added behind a host tool with a bare `skipUnless` is a skip
only CI refuses (issue #589 PR-1b; PR #557's five rows binding the credentials file).

What this script reproduces:

* HOME is a fresh empty directory.
* PATH keeps every directory that holds none of the hidden executables. A directory that holds
  one is replaced by a shim directory of symlinks to everything else in it, because dropping the
  whole directory also drops the linters installed beside a CLI and every lint row then fails.
* PYTHONPATH carries the user site-packages: under an empty HOME the interpreter no longer finds
  packages installed with `pip install --user`, pytest among them.
* Every `CLAUDE*` and `CODEX*` variable an agent session sets, and `PYTEST_ADDOPTS` /
  `PYTEST_PLUGINS`, are removed; `OMPI_MCA_rmaps_base_oversubscribe=1` is set, as CI's suite
  step sets it.
* CI's guard: a pristine collection (`-o addopts=`, nothing selecting) is counted first, and
  the run fails unless the report holds exactly that many `<testcase>` elements — so a subset
  selected by `pytest.ini`'s `addopts`, a plugin or anything else reads red, as it does in CI.
* The suite runs under `--junitxml`, and `python3 -m tools.tests.skip_probes` reads that
  report in the SAME environment, which is the host the check must run on (its docstring says
  why).

What it does NOT reproduce: CI runs the suite serially and this script runs it under
pytest-xdist by default (`--jobs 24`), so an order dependence red only serially is missed —
pass `--jobs 0` for CI's serial order. Every other host difference is inherited: other
compilers and profilers on PATH, which MPI comes first, and the rest of the environment.
A green result therefore means "green without the skip-probe dependencies and without the
operator's home", not "green in CI".

Run from the checkout root:

    python3 .claude/skills/atmofab-review-loop/scripts/ci_shape_suite.py [--jobs N] [--keep]

The temporary directory is removed at exit unless `--keep` is given.
Exit status: 0 only when the suite passed, the report holds the pristine number of tests, and
the skip check refused nothing; 2 when the run could not be set up.
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
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from pathlib import Path

#: environment variables the operator's session carries and CI does not
DROPPED_VARIABLES = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")
DROPPED_PREFIXES = ("CLAUDE", "CODEX")
#: set as `.github/workflows/tests.yml`'s suite step sets them
CI_VARIABLES = {"OMPI_MCA_rmaps_base_oversubscribe": "1"}


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


def ci_env(environ: Mapping[str, str], hide: set[str], shim: Path, home: Path,
           user_site: str) -> dict[str, str]:
    """The environment the suite runs in: `environ` with HOME, PATH, PYTHONPATH replaced."""
    env = {key: value for key, value in environ.items()
           if key not in DROPPED_VARIABLES and not key.startswith(DROPPED_PREFIXES)}
    env["HOME"] = str(home)
    env["PATH"] = ci_path(environ.get("PATH", ""), hide, shim)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (user_site, environ.get("PYTHONPATH")) if p)
    env.update(CI_VARIABLES)
    return env


def report_testcases(report: Path) -> int | None:
    """The number of `<testcase>` elements in a JUnit report, or None when there is none.

    Elements, not the `tests` attribute, which counts each `subTest` as a case of its own
    (the same reading as CI's guard step).
    """
    if not report.is_file():
        return None
    try:
        root = ET.parse(report).getroot()
    except ET.ParseError:
        return None
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    return sum(len(s.findall("testcase")) for s in suites)


def verdict(suite_rc: int, ran: int | None, expected: int, check_rc: int | None) -> int:
    """0 only when the suite passed, ran the pristine count, and the skip check refused nothing."""
    if suite_rc != 0 or ran is None or ran != expected or check_rc != 0:
        return 1
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=24,
                        help="pytest-xdist workers (default 24; 0 runs serially, as CI does)")
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary HOME, shim and report (removed by default)")
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


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run(args: argparse.Namespace, hide: set[str], root: Path,
         environ: Mapping[str, str] | None = None, runner: Runner = subprocess.run) -> int:
    shim, home, report = root / "shim", root / "home", root / "report.xml"
    shim.mkdir()
    home.mkdir()
    env = ci_env(os.environ if environ is None else environ, hide, shim, home,
                 site.getusersitepackages())
    still = sorted(name for name in hide if shutil.which(name, path=env["PATH"]))
    if still:
        print(f"still on the reduced PATH: {', '.join(still)}", file=sys.stderr)
        return 2
    print(f"hidden: {', '.join(sorted(hide))}; HOME={home}; jobs={args.jobs}")

    pristine = runner([sys.executable, "-m", "pytest", "tools/tests", "--collect-only", "-q",
                       "-o", "addopts=", "-p", "no:cacheprovider"],
                      env=env, capture_output=True, text=True, check=False)
    tail = [line for line in pristine.stdout.splitlines() if line.strip()]
    first = tail[-1].split()[0] if tail else ""
    if pristine.returncode != 0 or not first.isdigit():
        print("the pristine collection failed:\n" + pristine.stdout[-2000:], file=sys.stderr)
        return 2
    expected = int(first)

    cmd = [sys.executable, "-m", "pytest", "tools/tests", "-q", "-p", "no:randomly", "-rfEs",
           f"--junitxml={report}"]
    if args.jobs > 0:
        if importlib.util.find_spec("xdist") is None:
            print("pytest-xdist is not installed: pass --jobs 0", file=sys.stderr)
            return 2
        cmd += ["-n", str(args.jobs)]
    suite = runner(cmd, env=env, capture_output=True, text=True, check=False)
    lines = [line for line in suite.stdout.splitlines() if line.strip()]
    print(lines[-1] if lines else "(pytest printed nothing)")
    if suite.returncode != 0:
        for line in lines:
            if line.startswith(("FAILED", "ERROR")):
                print(line)
        print(suite.stderr[-2000:], file=sys.stderr)

    ran = report_testcases(report)
    print(f"ran={ran} expected={expected}")
    if ran is not None and ran != expected:
        print(f"the suite ran {ran} tests where a pristine collection counts {expected}: "
              "something selected a subset", file=sys.stderr)
    check_rc: int | None = None
    if report.is_file():
        check = runner([sys.executable, "-m", "tools.tests.skip_probes", str(report)],
                       env=env, capture_output=True, text=True, check=False)
        check_rc = check.returncode
        print(check.stdout[-4000:] if check_rc != 0 else
              next((ln for ln in check.stdout.splitlines() if ln.startswith("skipped=")),
                   check.stdout[-2000:]))
    else:
        print("no JUnit report was written: the skip check did not run", file=sys.stderr)
    return verdict(suite.returncode, ran, expected, check_rc)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
