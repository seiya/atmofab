#!/usr/bin/env python3
"""Every skip in the suite must name a declared environment capability.

The defect this closes: `test_real_full_fidelity_predicate_set_is_not_degenerate` pinned its
calibration input inside `workspace/`, the operator's execution workspace — gitignored, pruned at
the operator's discretion, absent from every fresh clone — and called
`skipTest("real IR artifact not present in this checkout")` when it was missing. It skipped
silently for weeks while its own comment declared it the real-shape calibration line. Nothing
watched, because a skip reads as "not applicable here" whether or not it is.

A test that quietly stops running is the class; that one was the instance. The rule is therefore
not about where inputs live — the first version of this guard tried to answer "which repository
path does this source construct", and three independent reviewers produced roughly twenty
spellings that evade an AST reading of path expressions (`.parent.parent.parent`, `.joinpath`,
f-strings, `os.path.join`, string concatenation, `glob`, an alias assigned on its own line), two
of them already in use in this tree. That question has no bounded answer at this level.

The question asked here instead is bounded: a skip's reason is a string literal, the set of them
is finite and enumerable, and every legitimate one names something about the host — a missing
binary, an unavailable kernel feature, a filesystem that cannot do symlinks, a privilege level.
A skip that names anything else is either a test that has stopped running or a capability nobody
declared, and both should be a failure rather than a line in the summary that no one reads.

Adding a genuinely new environment capability means adding it to `_DECLARED_ENVIRONMENT_SKIPS`
below, in the same commit as the skip, with a note saying what about the host it depends on.
That is the intended friction: it is one table, and it is the only place that grants permission.
"""

from __future__ import annotations

import ast
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tools" / "tests"

# The only reasons a test in this suite may stop running, each naming a property of the host.
# Not a list of tolerated exceptions: every entry must be a capability a different machine may
# genuinely lack, and `test_no_declared_reason_is_unused` fails if one stops being used.
_DECLARED_ENVIRONMENT_SKIPS = {
    "gfortran not available":
        "the Fortran front end is not installed on this host",
    "bwrap / user namespaces not available":
        "the sandbox runtime is absent or unprivileged user namespaces are disabled",
    "symlink not supported on this filesystem":
        "the checkout is on a filesystem without symlinks",
    "needs the checkout under $HOME":
        "the assertion is about paths below the home directory",
    "root bypasses file permissions":
        "running as root, where an unreadable-file test cannot be set up",
    "cannot make a file unreadable as this user":
        "the same privilege condition, from the other direction",
    "/etc/resolv.conf not resolvable on this host":
        "the host has no resolvable resolv.conf to bind-mount",
    "driver liveness probing requires Linux /proc":
        "procfs is absent (non-Linux host)",
    "driver identity capture requires Linux /proc":
        "procfs is absent (non-Linux host)",
    "requires Linux /proc":
        "procfs is absent (non-Linux host)",
    "boot_id comparison requires a readable /proc/sys/kernel/random/boot_id":
        "procfs is present but boot_id is not readable",
    "requires POSIX signals":
        "the platform has no SIGTERM",
}

# `skipUnless(condition, reason)` and `skipIf(condition, reason)` carry the reason second;
# every other form carries it first. `SkipTest` is the raised form of the same thing.
_REASON_INDEX = {
    "skipTest": 0, "skip": 0, "SkipTest": 0, "skipUnless": 1, "skipIf": 1, "skipif": 1,
}


def _tracked_test_modules() -> list[Path]:
    """Every Python module git tracks under `tools/tests/`, as the definition of the corpus."""
    out = subprocess.run(["git", "ls-files", "-z", "--", "tools/tests"], cwd=REPO_ROOT,
                         check=True, capture_output=True, text=True).stdout
    return sorted(REPO_ROOT / p for p in out.split("\0") if p.endswith(".py"))


def _skip_declarations(module: Path) -> list[tuple[int, str, str | None]]:
    """(line, form, reason) for every skip in one module. `reason` is None if not a literal.

    A non-literal reason is reported rather than ignored: a reason assembled at runtime cannot be
    audited against the table, so it is exactly the shape a silent skip would hide behind.
    """
    found: list[tuple[int, str, str | None]] = []

    def record(node: ast.Call, name: str) -> None:
        reason: ast.AST | None = None
        for kw in node.keywords:
            if kw.arg == "reason":
                reason = kw.value
        if reason is None:
            index = _REASON_INDEX[name]
            if len(node.args) > index:
                reason = node.args[index]
        if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
            found.append((node.lineno, name, reason.value))
        else:
            found.append((node.lineno, name, None))

    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func).split(".")[-1]
            if name in _REASON_INDEX:
                record(node, name)
    return found


def _violations(modules: list[Path]) -> list[str]:
    out: list[str] = []
    for module in modules:
        for line, form, reason in _skip_declarations(module):
            if reason is None:
                out.append(f"{module.name}:{line}: {form}() reason is not a string literal, "
                           "so it cannot be checked against the declared table")
            elif reason not in _DECLARED_ENVIRONMENT_SKIPS:
                out.append(f"{module.name}:{line}: {form}({reason!r}) is not a declared "
                           "environment capability")
    return out


class SkipReasonsAreDeclaredTests(unittest.TestCase):
    def test_every_skip_reason_is_a_declared_environment_capability(self) -> None:
        violations = _violations(_tracked_test_modules())
        self.assertEqual(violations, [], (
            "a test stops running for a reason nobody declared. If this is a real host "
            "capability, add it to _DECLARED_ENVIRONMENT_SKIPS with a note saying what about "
            "the host it depends on. If it is a missing repository file, capture the file into "
            f"tools/tests/data/ instead of skipping: {violations}"))

    def test_scan_covers_every_tracked_test_module(self) -> None:
        # The property that makes this a class guard is that it reads ALL of them. Deriving the
        # corpus from `git ls-files` rather than a glob means a narrowed scan is a failure here,
        # not a quietly smaller sweep — the first version of this file had a `rglob` that could
        # be replaced with a single module while every test stayed green.
        modules = _tracked_test_modules()
        on_disk = {p for p in TESTS_DIR.rglob("*.py")}
        tracked = set(modules)
        self.assertEqual(tracked - on_disk, set(), "tracked module missing from the checkout")
        self.assertEqual(on_disk - tracked, set(), (
            "an untracked .py sits in tools/tests/ — commit it or remove it; the scan is "
            "defined by what git tracks"))
        # Survives any single-point mutation by design: narrowing either side alone already
        # breaks the equality above. This is the backstop for narrowing both at once.
        self.assertGreater(len(modules), 30, f"corpus collapsed to {len(modules)} modules")

    def test_the_calibration_skip_this_guard_was_written_for_is_rejected(self) -> None:
        # The reject path, driven through the same `_violations` the scan uses, on the exact
        # reason string the calibration test used to carry. Without this nothing in the suite
        # exercises rejection: every module in the tree is compliant, so the scan passes whether
        # the comparison works or is stubbed out.
        src = ('import unittest\n'
               'class T(unittest.TestCase):\n'
               '    def test_x(self):\n'
               '        self.skipTest("real IR artifact not present in this checkout")\n')
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp) / "test_probe.py"
            module.write_text(src, encoding="utf-8")
            violations = _violations([module])
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("real IR artifact not present in this checkout", violations[0])
        self.assertIn("not a declared environment capability", violations[0])

    def test_a_declared_reason_passes_and_a_computed_one_does_not(self) -> None:
        declared = next(iter(_DECLARED_ENVIRONMENT_SKIPS))
        src = ('import unittest\n'
               'class T(unittest.TestCase):\n'
               f'    @unittest.skipUnless(HAVE, {declared!r})\n'
               '    def test_ok(self):\n'
               '        pass\n'
               '    def test_computed(self):\n'
               '        self.skipTest(f"missing {thing}")\n')
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp) / "test_probe.py"
            module.write_text(src, encoding="utf-8")
            violations = _violations([module])
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("not a string literal", violations[0])

    def test_every_skip_form_is_recognised(self) -> None:
        # One assertion per form, generated from the forms themselves: a form the extractor does
        # not know is a skip it cannot see, and checking them as one blob hides a single gap.
        forms = {
            "skipTest": '        self.skipTest({r})\n',
            "skip": '        unittest.skip({r})(lambda: None)\n',
            "skipUnless": '        unittest.skipUnless(False, {r})(lambda: None)\n',
            "skipIf": '        unittest.skipIf(True, {r})(lambda: None)\n',
            "skipif": '        pytest.mark.skipif(True, reason={r})\n',
            "SkipTest": '        raise unittest.SkipTest({r})\n',
        }
        reason = "undeclared reason for this form"
        for name, body in forms.items():
            src = ('import unittest, pytest\n'
                   'def f():\n' + body.format(r=repr(reason)))
            with tempfile.TemporaryDirectory() as tmp:
                module = Path(tmp) / "test_probe.py"
                module.write_text(src, encoding="utf-8")
                decls = _skip_declarations(module)
                violations = _violations([module])
            self.assertEqual([d[2] for d in decls], [reason], f"{name}: {decls}")
            self.assertEqual(len(violations), 1, f"{name}: {violations}")

    def test_no_declared_reason_is_unused(self) -> None:
        # A permission table that only grows is a table that stops meaning anything. Every entry
        # must correspond to a skip that exists; a removed skip takes its entry with it.
        used = {reason for module in _tracked_test_modules()
                for _, _, reason in _skip_declarations(module) if reason is not None}
        unused = sorted(set(_DECLARED_ENVIRONMENT_SKIPS) - used)
        self.assertEqual(unused, [], (
            f"declared environment capabilities no test skips on any more: {unused}"))


if __name__ == "__main__":
    unittest.main()
