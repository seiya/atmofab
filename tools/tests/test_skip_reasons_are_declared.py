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
(`_SKIP_APIS`), and membership in it — not the spelling of the call — is what decides, so an
unrelated local `def skip(...)` is not a skip while `from unittest import skipIf as omit_if`,
`unittest.case.SkipTest` and `from _pytest.outcomes import skip` all are. The last two are the
modules the APIs are really defined in, and that mapping is asked of the objects at import time
rather than listed, because listing it by hand is how the second of them got missed after the
first had been fixed. And a reason is a string literal, so the set of them is finite
and enumerable. Anything that names a skip API without handing over a declared literal reason — a
bare `@unittest.skip` decorator, `raise SkipTest` with no argument, `pytest.importorskip` without
`reason=`, a computed message — is a violation, so the check fails closed rather than falling
through to "not recognised, therefore fine".

**Scope, stated precisely, because an earlier version of this sentence claimed more than the
code does.** What is checked is *declared skips*: the APIs in `_SKIP_APIS`, by any spelling that
names them, plus `__unittest_skip__` set by hand in either the attribute or the class-body form.
A helper inside the corpus that takes the case as an argument is covered; one living outside
`tools/tests/` that raises `SkipTest` on the caller's behalf is not, and neither is anything
assembled at runtime — an API fetched with `getattr`, or a reason built at import time.

What is NOT covered, and cannot be by reading skip syntax: the other ways a test can fail to run
without declaring anything. `__test__ = False`, defining the test under an `if`, `del test_f`,
and an early `return` where the old code called `skipTest` — review demonstrated all four. The
last is the fixed defect with one word changed. They are out of reach here because they are not
skips; catching them needs a different instrument (observing what the runner actually executed),
and pretending otherwise in this docstring is how the previous version of it came to be false.

Two deliberate trades in the other direction. A class defining its own `skipTest` opts out of the
attribute-name match inside that class, since there the name no longer means the inherited
method — scoped to exactly that class body, because the radius was wrong twice: first per module,
where one test double with a `skipTest` silenced every real skip in the file, then per class but
descending into nested ones, which carried the opt-out into a real `TestCase` defined inside a
class that had the override. A nested class decides for itself. And `X.skipTest(...)` on any other
receiver IS matched, so an unrelated object with a method of that name would be reported —
accepted knowingly, because the alternative is missing every helper that takes the case as an
argument, and the fix for the false positive is visible and local.

Name resolution here is flow-insensitive: one map per module, built from its imports and from
assignments whose right-hand side resolves to an API, with no statement ordering. Rebinding a
bound name therefore gives a wrong answer in both directions — `def helper(unittest):
unittest.skip(...)` is reported though the parameter shadows the import, and `import unittest as
api` followed later by `import pytest as api` hides a real skip. Getting these right means
implementing Python's scope and ordering rules, which is a bigger instrument than the problem
justifies; they are written down here instead. The corpus contains neither.

Adding a genuinely new environment capability means adding it to `_DECLARED_ENVIRONMENT_SKIPS`
below, in the same commit as the skip, with a note saying what about the host it depends on.
That is the intended friction: it is one table, and it is the only place that grants permission.
"""

from __future__ import annotations

import ast
import importlib
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
    "pytest.mark.skip": 0,
    "pytest.mark.skipif": 1,
    "pytest.mark.xfail": None,
    "pytest.importorskip": None,
}
_SKIP_ROOTS = ("unittest", "pytest", "_pytest")


def _alternative_names() -> dict[str, str]:
    """Other dotted names for the same objects, asked of the objects rather than guessed.

    `unittest.skip` really lives in `unittest.case`, and `pytest.skip` in `_pytest.outcomes`, so
    `from unittest.case import SkipTest` and `from _pytest.outcomes import skip` name the very
    same functions. Review walked a skip past two earlier versions of this file through those
    two spellings, the second after the first had been hand-patched — which is the argument for
    deriving this from `__module__` instead of listing the ones someone happened to think of.
    """
    out: dict[str, str] = {}
    for canonical in _SKIP_APIS:
        root, _, rest = canonical.partition(".")
        if not rest or canonical.startswith("unittest.TestCase"):
            continue
        try:
            obj: object = importlib.import_module(root)
        except ImportError:  # pragma: no cover - pytest is present wherever this runs
            continue
        for part in rest.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                break
        defining = getattr(obj, "__module__", None)
        if defining and defining != root:
            out[f"{defining}.{rest.rsplit('.', 1)[-1]}"] = canonical
    return out


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
                    if a.name == "*":
                        # A star import binds the API's own leaf name, so `@skip(...)` after
                        # `from unittest import *` is the API by a shorter spelling.
                        root = module.split(".")[0]
                        for api in _SKIP_APIS:
                            if api.startswith(f"{root}."):
                                out.setdefault(api.rsplit(".", 1)[1], api)
                    else:
                        out[a.asname or a.name] = f"{module}.{a.name}"

    # A second pass for names bound by assignment (`_omit = unittest.skip`), which reach the same
    # object without an import of their own. Run after the imports so the right-hand side can be
    # resolved, and repeated until nothing new appears so a chain of them resolves too.
    for _ in range(len(_SKIP_APIS)):
        grew = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
                api = _canonical(node.value, out)
                for t in node.targets:
                    if api is not None and isinstance(t, ast.Name) and out.get(t.id) != api:
                        out[t.id] = api
                        grew = True
        if not grew:
            break
    return out


def _canonical(node: ast.AST, aliases: dict[str, str], own_skiptest: bool = False) -> str | None:
    """The skip API a Name/Attribute refers to, or None.

    `skipTest` is matched on any receiver, not just `self`/`cls`: it is an inherited method, and
    a helper that takes the case as an argument (`def _need(tc): tc.skipTest(...)`) stops the
    test just as surely. `own_skiptest` turns that off for a module that defines its own
    `skipTest`, where the name no longer means the inherited one — the one construct where
    matching by attribute name would invent a skip that is not there.
    """
    if not isinstance(node, (ast.Name, ast.Attribute)):
        return None
    parts = ast.unparse(node).split(".")
    if len(parts) > 1 and parts[-1] == "skipTest" and not own_skiptest:
        name = "unittest.TestCase.skipTest"
        return name if name in _SKIP_APIS else None
    head = aliases.get(parts[0])
    if head is None:
        return None
    name = ".".join([head] + parts[1:])
    name = _ALTERNATIVE_NAMES.get(name, name)
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

    def expand(mapping: ast.Dict) -> None:
        # `{"a": 1, **{"reason": "..."}}` nests, and Python flattens it before the call sees it.
        # Reading only the outer level reports a declared reason as unreadable.
        for k, v in zip(mapping.keys, mapping.values):
            if k is None and isinstance(v, ast.Dict):
                expand(v)
            elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                kwargs[k.value] = v

    for kw in node.keywords:
        if kw.arg is not None:
            kwargs[kw.arg] = kw.value
        elif isinstance(kw.value, ast.Dict):
            expand(kw.value)
    return args, kwargs


_ALTERNATIVE_NAMES = _alternative_names()


def _skip_sites(module: Path) -> list[tuple[int, str, str | None]]:
    """(line, api, reason) for every place this module can stop a test.

    `reason` is None when the site hands over no auditable literal — a bare decorator, a bare
    `raise`, a missing `reason=`, or a message assembled at runtime. Those are violations rather
    than omissions: a skip whose reason cannot be read is exactly what this guard exists to catch.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    aliases = _alias_map(tree)

    # The `skipTest` opt-out belongs to the class that overrides it, not to the file and not to
    # whatever that class happens to contain. Two earlier versions got the radius wrong: first
    # module-wide, so one `def skipTest` on an unrelated test double silenced every real skip in
    # the file; then class-wide via `ast.walk`, which descends into nested classes and so carried
    # the opt-out into a real `TestCase` defined inside one. Reviewers walked the retired
    # calibration reason through both. The descent stops at each class boundary and each class
    # decides for itself.
    opted_out: set[int] = set()

    def mark(scope: ast.AST, inherited: bool) -> None:
        own = inherited
        if isinstance(scope, ast.ClassDef):
            own = any(isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and b.name == "skipTest" for b in scope.body)
        for child in ast.iter_child_nodes(scope):
            if isinstance(child, ast.ClassDef):
                mark(child, False)
            else:
                if own:
                    opted_out.add(id(child))
                mark(child, own)

    mark(tree, False)
    found: list[tuple[int, str, str | None]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            api = _canonical(node.func, aliases, id(node) in opted_out)
            if api is None:
                continue
            args, kwargs = _literal_args(node)
            run = kwargs.get("run")
            if api == "pytest.mark.xfail" and not (
                    isinstance(run, ast.Constant) and run.value is False):
                continue  # xfail still runs the test; only `run=False` stops it from running
            reason = kwargs.get("reason")
            index = _SKIP_APIS[api]
            if reason is None and index is not None and len(args) > index:
                reason = args[index]
            if (api == "unittest.TestCase.skipTest"
                    and not isinstance(reason, ast.Constant) and len(args) > 1):
                # The unbound spelling `type(self).skipTest(self, reason)` puts the case first.
                reason = args[1]
            if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
                found.append((node.lineno, api, reason.value))
            else:
                found.append((node.lineno, api, None))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            # `__unittest_skip__` is the flag `TestCase.run` actually reads, set as
            # `test_x.__unittest_skip__ = True` after the definition, as a bare
            # `__unittest_skip__ = True` in a class body, and with an annotation on either.
            # Review walked a skip past earlier versions through the second and third spellings.
            # `__unittest_skip_why__` on its own skips nothing, and `= False` un-skips, so
            # neither is a violation: flagging them would report a test that does run.
            if node.value is None:
                continue  # a bare annotation binds nothing
            truthy = not (isinstance(node.value, ast.Constant) and not node.value.value)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                name = t.attr if isinstance(t, ast.Attribute) else \
                    t.id if isinstance(t, ast.Name) else None
                if name == "__unittest_skip__" and truthy:
                    found.append((node.lineno, "__unittest_skip__ metadata", None))

    # Bare references: a decorator or a `raise` that names a skip API without calling it stops a
    # test just as effectively and carries no reason at all. Only these two positions count — a
    # mention in an `except` clause or an `assertRaises` argument is not a skip site. A called
    # form arrives here as an `ast.Call`, which `_canonical` declines, so the two loops cannot
    # double-count and no bookkeeping between them is needed.
    for node in ast.walk(tree):
        bare: list[ast.AST] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bare = list(node.decorator_list)
        elif isinstance(node, ast.Raise) and node.exc is not None:
            bare = [node.exc]
        for ref in bare:
            api = _canonical(ref, aliases, id(ref) in opted_out)
            if api == "pytest.mark.xfail":
                continue  # bare `@pytest.mark.xfail` runs the test; only `run=False` stops it
            if api is not None:
                found.append((getattr(ref, "lineno", node.lineno), api, None))
    return sorted(found)


def _tracked_test_modules(root: Path = REPO_ROOT) -> list[Path]:
    """Every Python module git tracks under `tools/tests`, as the definition of the corpus.

    `root` exists so this can be driven against a scratch repository. Without that, the
    scan test compares two sets it derives the same way, and swapping this whole body for a glob
    makes the comparison tautological while an untracked module slips into the sweep.
    """
    out = subprocess.run(["git", "ls-files", "-z", "--", "tools/tests"], cwd=root,
                         check=True, capture_output=True, text=True).stdout
    return sorted(root / p for p in out.split("\0") if p.endswith(".py"))


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


_UNDECLARED = "not a capability of any host"
_NO_LITERAL = "without a literal reason"
_NOT_DECLARED = "not a declared environment capability"

# One usage per API, with `{r}` where the reason goes, so the same source can be run twice: once
# with a declared reason, which must be accepted, and once with an undeclared one, which must be
# rejected *for that reason*. Both halves matter. Without the accept half, a wrong reason index
# is invisible — ten single-site mutations of `_SKIP_APIS` and the argument handling survived a
# version of this file that only counted violations, and one of them would have started
# rejecting legitimate declared skips. `test_every_api_has_a_usage` keeps the table honest.
_USAGES = {
    "unittest.TestCase.skipTest":
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_x(self):\n        self.skipTest({r})\n",
    "unittest.skip":
        "import unittest\n@unittest.skip({r})\ndef test_x(): pass\n",
    "unittest.SkipTest":
        "import unittest\ndef test_x():\n    raise unittest.SkipTest({r})\n",
    "unittest.skipIf":
        "import unittest\n@unittest.skipIf(True, {r})\ndef test_x(): pass\n",
    "unittest.skipUnless":
        "import unittest\n@unittest.skipUnless(False, {r})\ndef test_x(): pass\n",
    "pytest.skip":
        "import pytest\ndef test_x():\n    pytest.skip({r})\n",
    "pytest.xfail":
        "import pytest\ndef test_x():\n    pytest.xfail({r})\n",
    "pytest.mark.skip":
        "import pytest\n@pytest.mark.skip({r})\ndef test_x(): pass\n",
    "pytest.mark.skipif":
        "import pytest\n@pytest.mark.skipif(True, {r})\ndef test_x(): pass\n",
    "pytest.mark.xfail":
        "import pytest\n@pytest.mark.xfail(run=False, reason={r})\ndef test_x(): pass\n",
    "pytest.importorskip":
        "import pytest\ndef test_x():\n    pytest.importorskip('mod', reason={r})\n",
}


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

    def test_every_api_has_a_usage(self) -> None:
        self.assertEqual(sorted(_USAGES), sorted(_SKIP_APIS),
                         "every skip API needs a usage, or the two tests below skip it silently")

    def test_a_declared_reason_is_accepted_through_every_api(self) -> None:
        # The half that pins WHERE the reason is read from. A wrong argument index, a dropped
        # keyword, or a `None` where a position belongs makes the reason unreadable, and without
        # this the result is a guard that quietly starts rejecting legitimate declared skips.
        declared = next(iter(_DECLARED_ENVIRONMENT_SKIPS))
        for api, template in _USAGES.items():
            self.assertEqual(_probe(template.format(r=repr(declared))), [], api)

    def test_an_undeclared_reason_is_rejected_through_every_api(self) -> None:
        # The other half. Asserting the message, not just the count: both rejection causes yield
        # exactly one violation, so counting alone cannot tell "undeclared" from "unreadable" —
        # and a mutation that turns one into the other then survives.
        for api, template in _USAGES.items():
            violations = _probe(template.format(r=repr(_UNDECLARED)))
            self.assertEqual(len(violations), 1, f"{api}: {violations}")
            self.assertIn(api, violations[0])
            self.assertIn(_UNDECLARED, violations[0])
            self.assertIn(_NOT_DECLARED, violations[0])

    def test_a_route_that_carries_no_readable_reason_is_rejected_as_such(self) -> None:
        # Distinct from the above: these stop a test while handing over nothing to check. Each
        # was a live evasion of an earlier version of this file, so they are listed one per line
        # rather than checked as a blob — a route that stops being seen must name itself.
        routes = {
            "bare decorator":
                'import unittest\n@unittest.skip\ndef test_x(): pass\n',
            "bare decorator on a class":
                'import unittest\n@unittest.skip\nclass T(unittest.TestCase): pass\n',
            "bare raise":
                'import unittest\ndef test_x():\n    raise unittest.SkipTest\n',
            "importorskip without a reason":
                'import pytest\ndef test_x():\n    pytest.importorskip("missing_package")\n',
            "reason computed at runtime":
                'import unittest\n'
                'class T(unittest.TestCase):\n'
                '    def test_x(self):\n        self.skipTest(f"missing {thing}")\n',
            "reason that is not a string":
                'import unittest\n'
                'class T(unittest.TestCase):\n'
                '    def test_x(self):\n        self.skipTest(404)\n',
            "hand-set unittest metadata, attribute form":
                'import unittest\n'
                'def test_x(): pass\n'
                'test_x.__unittest_skip__ = True\n',
            "hand-set unittest metadata, class-body form":
                'import unittest\n'
                'class T(unittest.TestCase):\n'
                '    __unittest_skip__ = True\n'
                '    __unittest_skip_why__ = "undeclared"\n'
                '    def test_x(self): pass\n',
            "hand-set unittest metadata, annotated":
                'import unittest\n'
                'def test_x(): pass\n'
                'test_x.__unittest_skip__: bool = True\n',
            "xfail that does not run the test":
                'import pytest\n@pytest.mark.xfail(run=False)\ndef test_x(): pass\n',
            "bare decorator on an async def":
                'import unittest\n@unittest.skip\nasync def test_x(): pass\n',
            "called with no arguments at all":
                'import unittest\n'
                'class T(unittest.TestCase):\n'
                '    def test_x(self):\n        self.skipTest()\n',
            "called with the condition but no reason":
                'import unittest\n@unittest.skipIf(True)\ndef test_x(): pass\n',
        }
        for name, src in routes.items():
            violations = _probe(src)
            self.assertEqual(len(violations), 1, f"{name}: {violations}")
            self.assertIn(_NO_LITERAL, violations[0], name)

    def test_the_api_is_recognised_by_identity_not_by_spelling(self) -> None:
        # Longer and shorter names for the same objects. `unittest.case` is where these are
        # defined, and review walked a skip past this file through that spelling with the
        # retired calibration reason — the same class of miss that killed the previous guard.
        for label, src in {
            "aliased import":
                'from unittest import skipIf as omit_if\n'
                f'@omit_if(True, {_UNDECLARED!r})\ndef test_x(): pass\n',
            "renamed module":
                'import unittest as u\n'
                f'def test_x():\n    raise u.SkipTest({_UNDECLARED!r})\n',
            "defining module named explicitly":
                'import unittest\n'
                f'def test_x():\n    raise unittest.case.SkipTest({_UNDECLARED!r})\n',
            "imported from the defining module":
                'from unittest.case import SkipTest\n'
                f'def test_x():\n    raise SkipTest({_UNDECLARED!r})\n',
            "submodule imported directly":
                'import unittest.case\n'
                f'def test_x():\n    raise unittest.case.SkipTest({_UNDECLARED!r})\n',
            "imported inside the function that skips":
                'def test_x():\n'
                '    import unittest\n'
                f'    raise unittest.SkipTest({_UNDECLARED!r})\n',
            "star import":
                'from unittest import *\n'
                f'@skip({_UNDECLARED!r})\ndef test_x(): pass\n',
            "the module the API is really defined in":
                'from _pytest.outcomes import skip\n'
                f'def test_x():\n    skip({_UNDECLARED!r})\n',
            "alias bound by assignment":
                'import unittest\n'
                '_omit = unittest.skip\n'
                f'@_omit({_UNDECLARED!r})\ndef test_x(): pass\n',
            "a TestCase nested inside a class that overrides skipTest":
                'import unittest\n'
                'class _Outer:\n'
                '    def skipTest(self, reason):\n        return None\n'
                '    class Inner(unittest.TestCase):\n'
                '        def test_x(self):\n'
                f'            self.skipTest({_UNDECLARED!r})\n',
            "case passed to a helper":
                'def _need(tc):\n'
                f'    tc.skipTest({_UNDECLARED!r})\n',
            "unbound call with the case as first argument":
                'import unittest\n'
                'class T(unittest.TestCase):\n'
                '    def test_x(self):\n'
                f'        type(self).skipTest(self, {_UNDECLARED!r})\n',
            "an unrelated class overriding skipTest does not silence this one":
                'import unittest\n'
                'class _Double:\n'
                '    def skipTest(self, reason):\n        return None\n'
                'class T(unittest.TestCase):\n'
                '    def test_x(self):\n'
                f'        self.skipTest({_UNDECLARED!r})\n',
        }.items():
            violations = _probe(src)
            self.assertEqual(len(violations), 1, f"{label}: {violations}")
            self.assertIn(_NOT_DECLARED, violations[0], label)

    def test_things_that_are_not_skips_are_not_reported(self) -> None:
        # The cost of a false positive is the guard being switched off, so the other direction
        # needs pinning too. An unrelated local `skip`, a mention in an `except` clause or as a
        # value, and a class that defines its own `skipTest` — for which the inherited meaning
        # no longer holds — must all stay silent.
        for label, src in {
            "unrelated local function":
                'def skip(reason):\n    return reason\n'
                'def helper():\n    return skip("not a skip at all")\n',
            "mentioned, not raised":
                'import unittest\n'
                'def helper():\n'
                '    try:\n        pass\n'
                '    except unittest.SkipTest:\n        pass\n'
                '    return unittest.SkipTest\n',
            "module defines its own skipTest":
                'import unittest\n'
                'class T(unittest.TestCase):\n'
                '    def skipTest(self, reason):\n        return None\n'
                '    def test_x(self):\n        self.skipTest("not the inherited one")\n',
            "metadata that un-skips":
                'import unittest\n'
                'def test_x(): pass\n'
                'test_x.__unittest_skip__ = False\n',
            "only the why, which skips nothing on its own":
                'import unittest\n'
                'class T(unittest.TestCase):\n'
                '    __unittest_skip_why__ = "explanatory, not a skip"\n'
                '    def test_x(self): pass\n',
            "xfail that still runs the test":
                'import pytest\n'
                '@pytest.mark.xfail(reason="expected to fail, but it does run")\n'
                'def test_x(): pass\n',
            "bare xfail, which also still runs the test":
                'import pytest\n@pytest.mark.xfail\ndef test_x(): assert False\n',
            "a bare name that happens to be skipTest":
                f'def test_x():\n    skipTest({_UNDECLARED!r})\n',
            "an async override of skipTest opts its class out":
                'import unittest\n'
                'class T(unittest.TestCase):\n'
                '    async def skipTest(self, reason):\n        return None\n'
                '    def test_x(self):\n        self.skipTest("not the inherited one")\n',
            "an annotation that binds nothing":
                'import unittest\n'
                'def test_x(): pass\n'
                'test_x.__unittest_skip__: bool\n',
        }.items():
            self.assertEqual(_probe(src), [], label)

    def test_a_skipTest_nested_in_a_method_does_not_opt_its_class_out(self) -> None:
        # The opt-out is a class overriding the method, not the name appearing somewhere inside
        # it. A local helper called `skipTest` defined in one test must not disarm the next.
        violations = _probe('import unittest\n'
                            'class T(unittest.TestCase):\n'
                            '    def test_helper(self):\n'
                            '        def skipTest(reason):\n            return None\n'
                            '    def test_x(self):\n'
                            f'        self.skipTest({_UNDECLARED!r})\n')
        self.assertEqual(len(violations), 1, violations)
        self.assertIn(_NOT_DECLARED, violations[0])

    def test_a_declared_reason_reaches_the_table_through_nested_unpacking(self) -> None:
        # Python flattens `{**{...}}` before the call sees it; reading only the outer level
        # reports a declared reason as unreadable, which rejects a legitimate skip.
        declared = next(iter(_DECLARED_ENVIRONMENT_SKIPS))
        self.assertEqual(_probe(
            'import unittest\n'
            f'@unittest.skipIf(**{{"condition": True, **{{"reason": {declared!r}}}}})\n'
            'def test_x(): pass\n'), [])

    def test_a_declared_reason_survives_literal_argument_unpacking(self) -> None:
        # Recovering these as "not a literal" would reject a valid skip, which teaches people to
        # route around the guard rather than to declare their capability.
        declared = next(iter(_DECLARED_ENVIRONMENT_SKIPS))
        for spelling in (f'*[True, {declared!r}]',
                         f'*(True, {declared!r})',
                         f'True, **{{"reason": {declared!r}}}'):
            self.assertEqual(_probe('import unittest\n'
                                    f'@unittest.skipIf({spelling})\n'
                                    'def test_x(): pass\n'), [], spelling)

    def test_the_corpus_comes_from_git_and_not_from_the_filesystem(self) -> None:
        # Driven against a scratch repository, because the scan test derives both of its sets
        # from this function: replacing the whole body with a glob makes that comparison
        # tautological, and an untracked module then joins the sweep unnoticed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools" / "tests").mkdir(parents=True)
            (root / "tools" / "tests" / "test_tracked.py").write_text("", encoding="utf-8")
            (root / "tools" / "tests" / "test_untracked.py").write_text("", encoding="utf-8")
            (root / "elsewhere.py").write_text("", encoding="utf-8")
            for cmd in (["git", "init", "-q"],
                        ["git", "add", "tools/tests/test_tracked.py", "elsewhere.py"]):
                subprocess.run(cmd, cwd=root, check=True, capture_output=True)
            found = _tracked_test_modules(root)
        self.assertEqual([p.name for p in found], ["test_tracked.py"])

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
