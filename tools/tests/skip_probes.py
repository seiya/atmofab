"""The only place a test in this suite is meant to skip from, and the reader of a run's reported skips.

Rule (issue #498): a test does not skip. The one exception is a dependency that GitHub-hosted
CI cannot reasonably install — today the CUDA compiler driver and the two leaf CLIs. Those
skip DYNAMICALLY: `present()` asks the host at run time, so a host that has the dependency
runs the row. Every other missing dependency FAILS with a message that names the install
(`docs/DEVELOPMENT.md` §Fresh-machine setup step 6). Widening `DEPENDENCIES` is an operator
decision, not a convenience; `check_report` refuses every reason not in it.

What decides is the RUN's report, not a reading of the test source: `check_report` reads a
pytest JUnit report and refuses every `<skipped>` element (one per skipped `subTest`, so a
`<testcase>` can carry several) that is

- an xfail that failed — pytest's JUnit writer reports `xfail` and `unittest.expectedFailure`
  as `<skipped type="pytest.xfail">`, the latter with an EMPTY message, so the type decides (a
  non-strict xfail that passes is reported as a pass: its body ran);
- a module skipped at collection — reported with no type and the message `collection skipped`,
  whatever reason it gave, so even a probe reason is refused there: a probe skip goes through
  `skip_unless_installed` on the class or function, or `require_installed` in the method;
- a skip whose message is not one of the probe reasons below;
- a probe-reason skip whose dependency is present on the host running the check — such a row
  could have run, so it must.

So the check must run on the host that ran the suite, with the same `PATH`.

    python3 -m pytest tools/tests -q -rs --junitxml=<report.xml>
    python3 -m tools.tests.skip_probes <report.xml>

It prints what it accepted, per reason with the test ids, then every refusal, and exits 1 on
any refusal. pytest collects only `test_*.py`, so this module is not itself a test; its tests
are `tools/tests/test_skip_probes.py`.

What it does not see. It matches the reason STRING, not where the skip came from: a test that
spells a probe reason itself is accepted wherever that dependency is absent (on CI, all three),
and refused only on a host that has it — the operator's, where all three are installed. And a
test that never reaches the report — an early `return` in a test body, `__test__ = False`, a test
under `if`, `del` of a test — is a pass or an absence, not a skip; nothing in the suite or in CI
sees those (CI's pristine collection count reads the same source as the run, so it drops them
on both sides). Both are accepted limits, recorded with their measurements on issue #498.
"""
from __future__ import annotations

import shutil
import sys
import unittest
import xml.etree.ElementTree as ET
from collections.abc import Callable

from tools.backends import registry


def _cuda_compiler_driver() -> str:
    # The compiler backend's own executable, as `tool_run_syntax_check` probes it; the
    # spelling stays in the backend (`docs/BACKEND_BOUNDARY.md`).
    return str(registry.capability_module("compiler", "nvcc", "syntax_check").EXECUTABLE)


#: dependency id -> (thunk naming the executable, the one reason string a skip for it carries)
DEPENDENCIES: dict[str, tuple[Callable[[], str], str]] = {
    "cuda_compiler_driver": (_cuda_compiler_driver, "the CUDA compiler driver is not installed"),
    "claude": (lambda: "claude", "the claude CLI is not installed on this host"),
    "codex": (lambda: "codex", "the codex CLI is not installed on this host"),
}
REASON_TO_DEPENDENCY: dict[str, str] = {reason: dep for dep, (_, reason) in DEPENDENCIES.items()}

# The type pytest's JUnit writer gives a skip; an xfail is `pytest.xfail` (measured, pytest 9.1.1).
_SKIP_TYPE = "pytest.skip"


def present(dependency: str) -> bool:
    executable, _ = DEPENDENCIES[dependency]
    return shutil.which(executable()) is not None


def skip_unless_installed(dependency: str):
    """Class or function decorator: skips with the dependency's fixed reason when it is absent."""
    return unittest.skipUnless(present(dependency), DEPENDENCIES[dependency][1])


def require_installed(test: unittest.TestCase, dependency: str) -> None:
    """In-method form, for a helper whose dependency is an argument."""
    if not present(dependency):
        test.skipTest(DEPENDENCIES[dependency][1])


def _case_id(case: ET.Element) -> str:
    classname, name = case.get("classname", ""), case.get("name", "")
    return f"{classname}::{name}" if classname else name


def _refusal(case_id: str, skipped: ET.Element, is_present: Callable[[str], bool]) -> str | None:
    """Why one `<skipped>` element is refused, or `None` when it is an accepted probe skip."""
    kind = skipped.get("type", "")
    message = skipped.get("message", "")
    if kind == "pytest.xfail":
        return (f"{case_id}: reported as <skipped type='pytest.xfail'> — an xfail or expected "
                "failure is a skip by another name; make the test pass or fail")
    if kind != _SKIP_TYPE:
        # A module-level `pytest.skip(..., allow_module_level=True)` or `importorskip` is
        # written with no type and the message `collection skipped` (measured, pytest 9.1.1).
        return (f"{case_id}: reported as <skipped type={kind!r}> with {message!r} — a module "
                "skipped at collection, or a shape this check does not know; a test in this "
                "suite does not skip, and a probe skip goes through skip_unless_installed or "
                "require_installed, not a module-level skip")
    dependency = REASON_TO_DEPENDENCY.get(message)
    if dependency is None:
        return (f"{case_id}: skipped with {message!r}, which is not a probe reason — a test in "
                "this suite does not skip; a missing dependency fails with a message naming the "
                "install (tools/tests/skip_probes.py)")
    if is_present(dependency):
        return (f"{case_id}: skipped with {message!r}, but {dependency} is installed on this "
                "host, so the row must run (or the check ran on another host or PATH than the "
                "suite)")
    return None


def check_report(path, *, present: Callable[[str], bool] | None = None
                 ) -> tuple[list[str], dict[str, list[str]]]:
    """(refusals, accepted ids by reason) over every `<testcase>` carrying `<skipped>`.

    `present` defaults to this module's probe, looked up at call time; a test stubs it.

    Raises on a missing or unparsable report, or one with no `<testsuite>`: an absent report
    is not "no skips"."""
    is_present = globals()["present"] if present is None else present
    root = ET.parse(path).getroot()
    if root.tag != "testsuite" and root.find("testsuite") is None:
        raise ValueError(f"{path}: no <testsuite> element; this is not a pytest JUnit report")
    refusals: list[str] = []
    accepted: dict[str, list[str]] = {}
    # Every `<testcase>` under the root, not only the first suite's: a case this loop does not
    # visit is a skip it accepts.
    for case in root.iter("testcase"):
        case_id = _case_id(case)
        # EVERY `<skipped>` child, not the first: pytest writes one per skipped `subTest` into
        # the same `<testcase>`, so a first-only read accepts the second subtest's reason unread.
        for skipped in case.findall("skipped"):
            refusal = _refusal(case_id, skipped, is_present)
            if refusal is not None:
                refusals.append(refusal)
            else:
                accepted.setdefault(skipped.get("message", ""), []).append(case_id)
    return refusals, accepted


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python3 -m tools.tests.skip_probes <junit-report.xml>", file=sys.stderr)
        return 2
    refusals, accepted = check_report(argv[0])
    total = sum(len(ids) for ids in accepted.values())
    print(f"skipped={total + len(refusals)} accepted={total} refused={len(refusals)}")
    for reason, ids in sorted(accepted.items()):
        print(f"accepted {len(ids)} skipped: {reason}")
        for case_id in ids:
            print(f"  {case_id}")
    for refusal in refusals:
        print(f"REFUSED {refusal}")
    return 1 if refusals else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
