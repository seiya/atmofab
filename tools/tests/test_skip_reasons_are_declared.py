#!/usr/bin/env python3
"""Every skip in the suite must name a declared environment capability.

The defect this closes: `test_real_full_fidelity_predicate_set_is_not_degenerate` pinned its
calibration input inside `workspace/`, the operator's execution workspace — gitignored, pruned at
the operator's discretion, absent from every fresh clone — and called
`skipTest("real IR artifact not present in this checkout")` when it was missing. It skipped
silently for weeks while its own comment declared it the real-shape calibration line. Nothing
watched, because a skip reads as "not applicable here" whether or not it is.

A test that quietly stops running is the class; that one was the instance. The rule is not about
where inputs live — the first version of this guard tried to answer "which repository path does
this source construct", and reviewers produced roughly twenty spellings that evade an AST reading
of path expressions, two already in use here. That question has no bounded answer at this level.

Two things make this one bounded. The set of APIs that can stop a test is closed and small
(`_SKIP_APIS`), and it is reachable only by naming one of them, so an alias is resolved through
the module's own imports rather than guessed from spelling. And a reason is a string literal, so
the set of them is finite and enumerable. Anything that names a skip API without handing over a
declared literal reason — a bare `@unittest.skip` decorator, `raise SkipTest` with no argument,
`pytest.importorskip` without `reason=`, a computed message — is a violation, so the check fails
closed rather than falling through to "not recognised, therefore fine".

What is still out of reach, and is not claimed otherwise: a decorator that sets
`__unittest_skip__` by hand is caught, but a helper living outside `tools/tests/` that raises
`SkipTest` on the caller's behalf is not, and neither is anything assembled at runtime. Those are
deliberate concealment rather than ordinary style, which is the line this guard draws.

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

# Canonical name -> index of the reason among the positional arguments. `skipUnless`/`skipIf`
# carry the condition first; everything else leads with the reason. `importorskip` takes the
# module name first and accepts a reason only by keyword, so it has no positional slot.
_SKIP_APIS = {
    "unittest.TestCase.skipTest": 0,
    "unittest.skip": 0,
    "unittest.SkipTest": 0,
    "unittest.skipIf": 1,
    "unittest.skipUnless": 1,
    "pytest.skip": 0,
    "pytest.xfail": 0,
    "pytest.mark.skipif": 1,
    "pytest.importorskip": None,
}
_SKIP_ROOTS = ("unittest", "pytest")


def _alias_map(tree: ast.AST) -> dict[str, str]:
    """Local name -> canonical dotted name, from this module's own imports.

    Resolving through the imports rather than matching the last name component is what makes
    `from unittest import skipIf as omit_if` visible and a local `def skip(...)` invisible. The
    previous version keyed on `ast.unparse(func).split(".")[-1]`, which got both backwards.
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in _SKIP_ROOTS:
                    out[a.asname or a.name.split(".")[0]] = a.name if a.asname else \
                        a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] in _SKIP_ROOTS:
                for a in node.names:
                    out[a.asname or a.name] = f"{module}.{a.name}"
    return out


def _canonical(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """The skip API a Name/Attribute refers to, or None."""
    if not isinstance(node, (ast.Name, ast.Attribute)):
        return None
    parts = ast.unparse(node).split(".")
    if parts[0] in ("self", "cls") and parts[1:] == ["skipTest"]:
        return "unittest.TestCase.skipTest"
    head = aliases.get(parts[0])
    if head is None:
        return None
    name = ".".join([head] + parts[1:])
    return name if name in _SKIP_APIS else None


def _literal_args(node: ast.Call) -> tuple[list[ast.AST], dict[str, ast.AST]]:
    """Positional and keyword arguments, expanding literal `*[...]` / `**{...}` unpacking."""
    args: list[ast.AST] = []
    kwargs: dict[str, ast.AST] = {}
    for a in node.args:
        if isinstance(a, ast.Starred):
            if isinstance(a.value, (ast.List, ast.Tuple)):
                args.extend(a.value.elts)
        else:
            args.append(a)
    for kw in node.keywords:
        if kw.arg is not None:
            kwargs[kw.arg] = kw.value
        elif isinstance(kw.value, ast.Dict):
            for k, v in zip(kw.value.keys, kw.value.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    kwargs[k.value] = v
    return args, kwargs


def _skip_sites(module: Path) -> list[tuple[int, str, str | None]]:
    """(line, api, reason) for every place this module can stop a test.

    `reason` is None when the site hands over no auditable literal — a bare decorator, a bare
    `raise`, a missing `reason=`, or a message assembled at runtime. Those are violations rather
    than omissions: a skip whose reason cannot be read is exactly what this guard exists to catch.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    aliases = _alias_map(tree)
    found: list[tuple[int, str, str | None]] = []
    called: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            api = _canonical(node.func, aliases)
            if api is None:
                continue
            called.add(id(node.func))
            args, kwargs = _literal_args(node)
            reason = kwargs.get("reason") or kwargs.get("msg")
            index = _SKIP_APIS[api]
            if reason is None and index is not None and len(args) > index:
                reason = args[index]
            if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
                found.append((node.lineno, api, reason.value))
            else:
                found.append((node.lineno, api, None))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr.startswith("__unittest_skip"):
                    found.append((node.lineno, "__unittest_skip__ metadata", None))

    # Bare references: a decorator or a `raise` that names a skip API without calling it stops a
    # test just as effectively and carries no reason at all. Only these two positions count — a
    # mention in an `except` clause or an `assertRaises` argument is not a skip site.
    for node in ast.walk(tree):
        bare: list[ast.AST] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bare = list(node.decorator_list)
        elif isinstance(node, ast.Raise) and node.exc is not None:
            bare = [node.exc]
        for ref in bare:
            if id(ref) in called:
                continue
            api = _canonical(ref, aliases)
            if api is not None:
                found.append((getattr(ref, "lineno", node.lineno), api, None))
    return sorted(found)


def _tracked_test_modules() -> list[Path]:
    """Every Python module git tracks under `tools/tests/`, as the definition of the corpus."""
    out = subprocess.run(["git", "ls-files", "-z", "--", "tools/tests"], cwd=REPO_ROOT,
                         check=True, capture_output=True, text=True).stdout
    return sorted(REPO_ROOT / p for p in out.split("\0") if p.endswith(".py"))


def _violations(modules: list[Path]) -> list[str]:
    out: list[str] = []
    for module in modules:
        if not module.is_file():
            out.append(f"{module.name}: tracked but not present in the checkout")
            continue
        for line, api, reason in _skip_sites(module):
            if reason is None:
                out.append(f"{module.name}:{line}: {api} stops a test without a literal reason "
                           "this table can check")
            elif reason not in _DECLARED_ENVIRONMENT_SKIPS:
                out.append(f"{module.name}:{line}: {api}({reason!r}) is not a declared "
                           "environment capability")
    return out


def _probe(src: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        module = Path(tmp) / "test_probe.py"
        module.write_text(src, encoding="utf-8")
        return _violations([module])


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
        on_disk = set(TESTS_DIR.rglob("*.py"))
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
        violations = _probe('import unittest\n'
                            'class T(unittest.TestCase):\n'
                            '    def test_x(self):\n'
                            '        self.skipTest("real IR artifact not present in this '
                            'checkout")\n')
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("real IR artifact not present in this checkout", violations[0])
        self.assertIn("not a declared environment capability", violations[0])

    def test_each_way_of_stopping_a_test_is_seen(self) -> None:
        # One case per route, generated from a table rather than checked as one blob: a route the
        # extractor cannot see is a skip that passes, and a single combined assertion hides which
        # one is missing. Every entry here was a live evasion of an earlier version of this file.
        undeclared = "undeclared reason for this route"
        routes = {
            "self.skipTest":
                f'    def test_x(self):\n        self.skipTest({undeclared!r})\n',
            "decorator, called":
                f'    @unittest.skipUnless(False, {undeclared!r})\n'
                '    def test_x(self): pass\n',
            "decorator, bare (no reason at all)":
                '    @unittest.skip\n'
                '    def test_x(self): pass\n',
            "raise, called":
                f'    def test_x(self):\n        raise unittest.SkipTest({undeclared!r})\n',
            "raise, bare (no reason at all)":
                '    def test_x(self):\n        raise unittest.SkipTest\n',
            "computed reason":
                '    def test_x(self):\n        self.skipTest(f"missing {thing}")\n',
            "hand-set unittest metadata":
                '    def test_x(self): pass\n'
                '    test_x.__unittest_skip__ = True\n',
        }
        for name, body in routes.items():
            violations = _probe('import unittest\n'
                                'class T(unittest.TestCase):\n' + body)
            self.assertEqual(len(violations), 1, f"{name}: {violations}")

    def test_an_alias_does_not_hide_a_skip_and_a_local_name_is_not_one(self) -> None:
        # Resolution goes through the module's imports. Aliasing the API must not conceal a skip,
        # and an unrelated local function that happens to be called `skip` must not invent one.
        aliased = _probe('from unittest import skipIf as omit_if\n'
                         '@omit_if(True, "aliased undeclared reason")\n'
                         'def test_x(): pass\n')
        self.assertEqual(len(aliased), 1, aliased)
        self.assertIn("unittest.skipIf", aliased[0])

        renamed_module = _probe('import unittest as u\n'
                                'class T(u.TestCase):\n'
                                '    def test_x(self):\n'
                                '        raise u.SkipTest("renamed module undeclared")\n')
        self.assertEqual(len(renamed_module), 1, renamed_module)

        local = _probe('def skip(reason):\n    return reason\n'
                       'def helper():\n    return skip("not a skip at all")\n')
        self.assertEqual(local, [])

        mention = _probe('import unittest\n'
                         'def helper():\n'
                         '    try:\n        pass\n'
                         '    except unittest.SkipTest:\n        pass\n'
                         '    return unittest.SkipTest\n')
        self.assertEqual(mention, [])

    def test_pytest_routes_and_argument_unpacking(self) -> None:
        # pytest is not imported anywhere in this suite today; these pin the routes before the
        # first one appears, when adding them would be someone else's problem to notice.
        self.assertEqual(len(_probe('import pytest\n'
                                    'pytest.importorskip("missing_package")\n')), 1)
        self.assertEqual(len(_probe('import pytest\n'
                                    'pytest.skip("undeclared pytest reason")\n')), 1)
        self.assertEqual(len(_probe('import pytest\n'
                                    '@pytest.mark.skipif(True, reason="undeclared marker")\n'
                                    'def test_x(): pass\n')), 1)
        # A declared reason delivered by literal unpacking is still a declared reason: recovering
        # it as "not a literal" would reject a valid skip and teach people to route around this.
        declared = next(iter(_DECLARED_ENVIRONMENT_SKIPS))
        self.assertEqual(_probe('import unittest\n'
                                f'@unittest.skipIf(*[True, {declared!r}])\n'
                                'def test_x(): pass\n'), [])
        self.assertEqual(_probe('import unittest\n'
                                f'@unittest.skipIf(True, **{{"reason": {declared!r}}})\n'
                                'def test_x(): pass\n'), [])

    def test_no_declared_reason_is_unused(self) -> None:
        # A permission table that only grows is a table that stops meaning anything. Every entry
        # must correspond to a skip that exists; a removed skip takes its entry with it.
        used = {reason for module in _tracked_test_modules() if module.is_file()
                for _, _, reason in _skip_sites(module) if reason is not None}
        unused = sorted(set(_DECLARED_ENVIRONMENT_SKIPS) - used)
        self.assertEqual(unused, [], (
            f"declared environment capabilities no test skips on any more: {unused}"))


if __name__ == "__main__":
    unittest.main()
