#!/usr/bin/env python3
"""The suite must give the same verdict under any operator environment (issue #84).

`tools/tests/conftest.py` removes the operator's per-run knobs from `os.environ` before
collection. That guard has the shape the conftest docstring warns about: nothing normally
trips it, so it can be deleted, inverted or quietly narrowed and every other test stays
green. Its sibling — the isolated-homes guard — was the only survivor of a reviewer's
18-mutant sweep for exactly that reason, and the answer there was a witness. This is that
witness for the environment half.

The end-to-end case carries a CONTROL that must fail. A subprocess that passes proves
nothing on its own: it cannot be told apart from a poisoning that was never poisonous. So
the same test is driven three ways — poisoned under pytest (passes, because the guard ran),
poisoned under plain `unittest` where no conftest is loaded (fails, which is what makes the
first result mean something), and clean under `unittest` (passes, which places the blame on
the environment rather than on the runner).
"""

from __future__ import annotations

import ast
import collections
import json
import os
import re
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools.tests.suite_env_guard import (
    BACKEND_CONFIG_HOME_ENV,
    MUST_BE_INHERITED,
    STRIPPED_OPERATOR_ENV,
    SUITE_OWNED_ENV,
    operator_env_names_to_strip,
    undeclared_operator_env_names,
)
import tools.tests.suite_env_guard as suite_env_guard

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The files whose comments this change owns, and whose test citations must lead somewhere.
_CITATION_SOURCES = (
    "tools/tests/suite_env_guard.py",
    "tools/tests/conftest.py",
    "tools/tests/test_suite_environment_isolation.py",
    "tools/tests/test_hooks_common.py",
)

# The regression issue #84 opens with: under the workflow the MCP server refuses a
# caller-named `repo_root`, and this test asserts the OUTSIDE-a-run branch. An operator
# with `METDSL_ORCHESTRATION_ID` exported got a failure belonging to no change.
_SUBJECT_MODULE = "tools.tests.test_build_runtime_server"
_SUBJECT_CLASS = "OrchestratedEnvAllowlistTests"
_SUBJECT_TEST = "test_only_an_absent_repo_root_falls_back_to_project_dir"

def _environment_names_read_by(repo_root: Path) -> set[str]:
    """Every upper-case environment name READ in non-test `tools/` and `mcp_servers/`.

    By AST, not by regex: `os.environ["X"]`, `os.environ.get("X")` and `os.getenv("X")` all
    count, and a name that only appears in a comment, a docstring or an assignment does
    not. That distinction is why three hand-counts of "the names the tree reads"
    disagreed — 17, 23 and 25 — each answering a different question about spellings.

    A read through a MODULE CONSTANT counts too — `_LIVENESS_TTL_ENV =
    "METDSL_ORCH_LIVENESS_TTL_SECONDS"` then `os.environ.get(_LIVENESS_TTL_ENV)`. Six names
    in this tree are spelled that way and a literal-only reader misses every one of them,
    which is one of the reasons the hand-counts disagreed. Resolved one level, against
    module-scope assignments in the same file; a name computed at runtime or imported from
    elsewhere is beyond a static reader and is NOT covered — stated rather than implied.
    """
    found: set[str] = set()
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", "tests")
                   and not d.startswith("workspace")]
        rel = Path(root).relative_to(repo_root)
        if not rel.parts or rel.parts[0] not in ("tools", "mcp_servers"):
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            try:
                tree = ast.parse((Path(root) / name).read_text(
                    encoding="utf-8", errors="replace"))
            except SyntaxError:                      # not this test's subject
                continue
            constants = {}
            for node in tree.body:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    constants[node.targets[0].id] = node.value.value

            def literal(expr) -> str | None:
                if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                    return expr.value
                if isinstance(expr, ast.Name):
                    return constants.get(expr.id)
                return None

            for node in ast.walk(tree):
                target = None
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("get", "getenv", "pop") and node.args):
                    base = node.func.value
                    if ((isinstance(base, ast.Attribute) and base.attr == "environ")
                            or (isinstance(base, ast.Name) and base.id in ("os", "environ"))):
                        target = literal(node.args[0])
                if (isinstance(node, ast.Subscript)
                        and isinstance(node.value, ast.Attribute)
                        and node.value.attr == "environ"):
                    target = literal(node.slice)
                if target and target.isupper():
                    found.add(target)
    return found


# Runs in a fresh interpreter: instrument `os.environ`, import, print what was read.
_IMPORT_SPY_SOURCE = '''\
import collections.abc, json, os, sys


class _Spy(collections.abc.MutableMapping):
    def __init__(self, real):
        self._real = real
        self.reads = []

    def __getitem__(self, key):
        # The FIRST frame under the root, not the immediate caller: `os.getenv` and
        # `MutableMapping.get` are stdlib frames between the read and whoever asked for
        # it, so reporting the caller named `<frozen _collections_abc>` and told nobody
        # anything. Falls back to the immediate caller if nothing is under the root.
        # The root is an ARGUMENT, not `os.getcwd()`: the synthetic probes live in a
        # temporary directory, so a cwd-anchored walk always missed and always fell back
        # — every self-test reported `<frozen _collections_abc>`, the very string this
        # walk exists to stop printing, and deleting the walk survived a sweep.
        root = sys.argv[3] if len(sys.argv) > 3 else os.getcwd()
        frame = sys._getframe(1)
        immediate = frame
        while frame is not None and not frame.f_code.co_filename.startswith(root):
            frame = frame.f_back
        frame = frame if frame is not None else immediate
        self.reads.append([key, frame.f_code.co_filename, frame.f_lineno])
        return self._real[key]

    def __setitem__(self, key, value):
        self._real[key] = value

    def __delitem__(self, key):
        del self._real[key]

    def __iter__(self):
        return iter(self._real)

    def __len__(self):
        return len(self._real)

    def copy(self):
        return self._real.copy()


spy = _Spy(os.environ)
os.environ = spy
sys.path.insert(0, os.getcwd())
if len(sys.argv) > 2:
    sys.path.insert(0, sys.argv[2])
__import__(sys.argv[1])
print(json.dumps(sorted({tuple(r) for r in spy.reads})))
'''


def _run(argv: list[str], env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    """A subprocess whose environment this test states in full, on the axis under test.

    The base is the parent's environment with every strippable name REMOVED before
    `env_extra` is applied. Inheriting them instead would make the clean control depend on
    the parent's own environment — which is exactly the coupling this file exists to close,
    and it showed up as a failing control the first time the suite was run with
    `--keep-operator-env`.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in set(operator_env_names_to_strip(os.environ))}
    env.update(env_extra)
    return subprocess.run(
        argv, cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=300)


class OperatorEnvironmentIsolationTests(unittest.TestCase):

    def test_no_ambient_operator_name_survives_into_a_running_test(self) -> None:
        """The guard's effect, observed from inside a test rather than inferred.

        TWO claims, because "no `METDSL_*` name is set during a test" is false and would
        be the wrong thing to pin:

        1. Nothing a test sees carries the OPERATOR's value. Vacuous on a clean host —
           there was nothing to strip — which is why the end-to-end case below drives a
           real poisoned process rather than relying on this one.
        2. Every `METDSL_*` name that IS set during a test is one the SUITE set, and is
           declared in `suite_env_guard.SUITE_OWNED_ENV`. That is a ratchet: a new process-global environment dependence
           cannot appear without someone naming it here and saying who sets it.

        Subset, not equality: `SUITE_OWNED_ENV` holds names set at another module's
        import, so which of them are present depends on what has been collected. Running
        this file alone shows one; running the whole suite shows both.
        """
        if "pytest" not in sys.modules:
            # The subject is a pytest hook. Under plain `unittest` there is no conftest
            # and nothing to observe; asserting anyway would fail for the absence of the
            # harness rather than for a defect.
            self.skipTest("the suite's environment guard is not installed")
        # Under pytest the flag is an ASSERTION, not a skip condition. Skipping on it
        # instead let "the hook never set CONFIGURED" pass as "not applicable" — measured,
        # that mutant survived a sweep by turning this test into a skip. Read from the
        # module the hook WRITES TO, so a reader that has ended up with a second copy of
        # the rule fails here rather than asserting over an empty record.
        self.assertTrue(suite_env_guard.CONFIGURED,
                        "the conftest hook did not run, or wrote to a different module")

        for name, operator_value in STRIPPED_OPERATOR_ENV.items():
            if name in SUITE_OWNED_ENV and os.environ.get(name) == operator_value:
                # Undecidable by VALUE, and only for these two. The suite sets them itself
                # AFTER the strip, so an operator who exported the value the suite happens
                # to use — `METDSL_DEP_READINESS_ALLOW_PERSISTED_FALLBACK=1` is the suite's
                # own — cannot be told apart here from the suite having set it.
                #
                # The exemption is narrow ON PURPOSE, and the first version of it was NOT.
                # Skipping these names unconditionally is how a real defect got read as a
                # collision: the guard was recording them without removing them, this loop
                # failed under a poisoned whole-suite run exactly as it should have, and
                # the exemption written to "fix" the collision would have silenced it. What
                # decides the case now is `strip_operator_env`'s own removal check, which
                # fails the session at the point of the strip where the question is not
                # ambiguous, plus the synthetic drive below.
                continue
            with self.subTest(name=name):
                self.assertNotEqual(
                    os.environ.get(name), operator_value,
                    f"{name} reached a test with the value the operator exported")

        undeclared = undeclared_operator_env_names(os.environ)
        if suite_env_guard.DECLINED:
            # The operator ran with --keep-operator-env, so their names ARE present and
            # that is what they asked for. Telling them the guard is broken sends them
            # after a defect that is their own flag — and this is exactly when it fires,
            # since the flag plus a knob set is the designed way to reach it.
            # Which side owns it is decidable: a name the operator exported is in the
            # process environment the flag preserved, and one the suite added is not
            # anything the operator set. Saying "the knob you set" for a name the SUITE
            # introduced is the mirror image of the misdirection this branch fixed one
            # round ago, so both possibilities are named rather than one asserted.
            self.assertEqual(
                undeclared, set(),
                "--keep-operator-env left the operator's environment in place, so a name "
                "you exported belongs to the knob and not to the suite — unset it or drop "
                "the flag. If you did NOT export it, the suite grew a new process-global "
                "environment dependence and it needs declaring in "
                f"`suite_env_guard.SUITE_OWNED_ENV`: {sorted(undeclared)}")
        else:
            self.assertEqual(
                undeclared, set(),
                "an environment name is set during a test and nobody says who sets it. If "
                "the suite sets it, declare it in `suite_env_guard.SUITE_OWNED_ENV` with "
                "the site that does; if it leaked from the operator, the guard in "
                "tools/tests/suite_env_guard.py is broken. Values: "
                f"{({n: os.environ[n] for n in undeclared})}")

    def test_the_ratchet_reports_a_name_nobody_has_claimed(self) -> None:
        """The ratchet's own witness — it fires on nothing this suite produces.

        Without this, `undeclared_operator_env_names` returning a constant empty set is
        invisible: no test sets an undeclared name, so the check above passes either way.
        Driven on a synthetic mapping, since the real one is the thing being ratcheted.
        """
        declared = dict.fromkeys(SUITE_OWNED_ENV, "x")
        self.assertEqual(undeclared_operator_env_names(declared), set())
        self.assertEqual(
            undeclared_operator_env_names({**declared, "METDSL_A_NEW_KNOB": "1"}),
            {"METDSL_A_NEW_KNOB"})
        self.assertEqual(
            undeclared_operator_env_names({**declared, "CODEX_HOME": "/tmp/x"}),
            {"CODEX_HOME"})

    def test_the_strip_removes_the_name_and_records_what_it_removed(self) -> None:
        """Both halves, because a mutant that does one and not the other survives.

        Dropping the record while still popping leaves the environment correct and the
        WITNESS above vacuous — the same shape as the module split this file was written
        after, reached a second way. Driven on a synthetic mapping: on a clean host the
        real strip removes nothing, so there is nothing for an in-process assertion to see.
        """
        stack = suite_env_guard.isolated_record()
        stack.__enter__()
        self.addCleanup(stack.__exit__, None, None, None)

        owned = sorted(SUITE_OWNED_ENV)[0]
        # A SUITE_OWNED_ENV name is in the mapping deliberately: the strip must remove
        # those too — the table exempts them from the RATCHET, not from the guard — and a
        # mutant that records one without popping it is the shape a reviewer found
        # surviving. Here nothing else writes to the mapping, so it is decidable.
        environ = {"METDSL_A_KNOB": "1", "CODEX_HOME": "/tmp/x", owned: "9", "PATH": "/b"}
        removed = suite_env_guard.strip_operator_env(environ)
        self.assertEqual(removed, sorted(["CODEX_HOME", "METDSL_A_KNOB", owned]))
        self.assertEqual(environ, {"PATH": "/b"})
        self.assertEqual(suite_env_guard.STRIPPED_OPERATOR_ENV,
                         {"METDSL_A_KNOB": "1", "CODEX_HOME": "/tmp/x", owned: "9"})
        suite_env_guard.restore_operator_env(environ)
        self.assertEqual(environ, {"METDSL_A_KNOB": "1", "CODEX_HOME": "/tmp/x",
                                   owned: "9", "PATH": "/b"})
        self.assertEqual(suite_env_guard.STRIPPED_OPERATOR_ENV, {})

    def test_the_rule_is_read_from_the_leaf_env_prefix_constant_not_copied(self) -> None:
        """Coupled by POINTER: the prefix is defined once, in the production constant.

        `orchestration_runtime.LEAF_ENV_ALLOWED_PREFIXES` is what decides which host names
        reach a leaf; a `METDSL_*` knob added later must be neutralized here without
        anyone remembering this file exists. Pinning the string `"METDSL_"` in a second
        place would state the rule twice and let the two drift, so what is pinned is that
        the function FOLLOWS the constant — moving the constant moves the answer.
        """
        import tools.orchestration_runtime as runtime

        sample = {"METDSL_ORCHESTRATION_ID": "x", "ZZTOP_KNOB": "y", "PATH": "/b"}
        self.assertEqual(operator_env_names_to_strip(sample), ["METDSL_ORCHESTRATION_ID"])
        with mock.patch.object(runtime, "LEAF_ENV_ALLOWED_PREFIXES", ("ZZTOP_",)):
            self.assertEqual(operator_env_names_to_strip(sample), ["ZZTOP_KNOB"])

    def test_the_backend_configuration_homes_are_covered_too(self) -> None:
        """`CODEX_HOME` carries no `METDSL_` prefix and cost 10 failures when exported.

        Measured on `165c26f` over `tools/tests/test_orchestration_runtime.py` alone.
        `CLAUDE_CONFIG_DIR` is its twin; it cost 0 in the same measurement and is covered
        by symmetry, so that a future test reading it cannot inherit the operator's.
        """
        self.assertEqual(BACKEND_CONFIG_HOME_ENV, ("CODEX_HOME", "CLAUDE_CONFIG_DIR"))
        sample = {name: "/tmp/x" for name in BACKEND_CONFIG_HOME_ENV}
        self.assertEqual(operator_env_names_to_strip(sample),
                         sorted(BACKEND_CONFIG_HOME_ENV))

    def test_every_environment_name_the_tree_reads_is_stripped_or_declared(self) -> None:
        """The ratchet that replaces a count three enumerations disagreed on.

        The first version of this test collected `METDSL_*` LITERALS with a regex and
        asked whether the strip covers them. Two reviewers found the same thing from
        different angles: the regex harvests only names beginning with `METDSL_` and the
        rule covers exactly names beginning with `METDSL_`, so the equality was true by
        construction. Adding `GEMINI_CONFIG_DIR` or `METDSL2_NEW_KNOB` to the tree — both
        uncovered — left it green. It pinned nothing about the tree, and its docstring
        said it could not go stale.

        What can actually go stale is the half that is HAND-WRITTEN:
        `BACKEND_CONFIG_HOME_ENV`. `CODEX_HOME` and `CLAUDE_CONFIG_DIR` carry no prefix, so
        a knob added tomorrow under a third spelling — another backend's configuration
        home, a new vendor variable — is covered by nothing and nobody is told.

        So the question asked here is the one that can fail: every name the tree READS from
        the environment must be either stripped, or declared below as something the suite
        must inherit. Read by AST rather than by regex, so `os.environ["X"]`,
        `os.environ.get("X")` and `os.getenv("X")` all count and a mention in a comment
        does not.
        """
        read = _environment_names_read_by(_REPO_ROOT)
        self.assertTrue(read, "the enumeration found nothing — the walk is broken")

        # BREADTH. The walk's scope is an input to the answer, and nothing else pins it:
        # measured, pruning `tools/hooks` and `tools/backends`, or narrowing the file
        # filter to one module, left the previous version of this test green. These four
        # names live in four different files under both scanned roots, so a scope that
        # stops covering one of them fails here instead of quietly shrinking the question.
        # `PYTHONPATH` is the anchor for the `mcp_servers` root because it is read ONLY
        # there (measured: of the 27 names, only it and PYTHONDONTWRITEBYTECODE are). The
        # first version anchored that root on `METDSL_ORCHESTRATION_ID`, which is also read
        # under `tools/`, so dropping the whole `mcp_servers` root survived — the exact
        # silent shrinkage this block exists to stop.
        for name, where in (("METDSL_HOOK_REPO_ROOT", "tools/hooks/cli.py"),
                            ("PYTHONPATH", "mcp_servers/build_runtime_server.py"),
                            ("METDSL_ORCH_LIVENESS_TTL_SECONDS", "tools/validate_workspace_root.py"),
                            ("METDSL_START_CLAIM_ROOT", "tools/run_workflow.py")):
            # NOT PINNED BY ANYTHING ITSELF: removing these four assertions survives a
            # sweep, because an assertion inside a test is not a mechanism. The regress
            # stops here — what it buys is that the walk's scope cannot shrink silently,
            # which the previous version of this test allowed.
            self.assertIn(name, read, f"the walk no longer reaches {where}")

        undecided = suite_env_guard.undecided_environment_names(read)
        self.assertEqual(
            undecided, set(),
            "an environment name is read by this tree and is neither stripped by the "
            "operator-environment guard nor declared as something the suite must inherit. "
            "Decide which it is: add it to the guard if it is a per-run knob an operator "
            "may have exported, or to MUST_BE_INHERITED with the reason it has to survive."
            f" Names: {sorted(undecided)}")

    def test_the_environment_name_reader_sees_all_three_spellings(self) -> None:
        """Each spelling the docstring claims, driven on a synthetic tree.

        Deleting the `ast.Subscript` clause survived a reviewer's sweep: this tree spells
        `os.environ["X"]` exactly once and the one name it loses is already declared
        inheritable, so the ratchet that consumes the reader stayed green. A branch the
        corpus exercises once is a branch a corpus measurement cannot pin.
        """
        with tempfile.TemporaryDirectory() as td:
            pkg = Path(td) / "tools"
            pkg.mkdir()
            (pkg / "probe.py").write_text(
                'import os\n'
                '_VIA_CONSTANT = "PROBE_CONSTANT"\n'
                'a = os.environ.get("PROBE_GET")\n'
                'b = os.environ["PROBE_SUBSCRIPT"]\n'
                'c = os.getenv("PROBE_GETENV")\n'
                'd = os.environ.get(_VIA_CONSTANT)\n'
                'def f(name):\n    return os.environ.get(name)\n',
                encoding="utf-8")
            found = _environment_names_read_by(Path(td))
        self.assertEqual(
            found,
            {"PROBE_GET", "PROBE_SUBSCRIPT", "PROBE_GETENV", "PROBE_CONSTANT"},
            "one of the spellings the reader claims to cover is not covered — or a "
            "runtime-computed name, which it states it cannot see, was reported")

    def test_the_strip_refuses_to_report_success_without_removing(self) -> None:
        """`strip_operator_env`'s own removal check, driven where it can fire.

        It exists because this branch shipped a mutant that recorded names without
        removing them, and survived a whole-suite run. On a real `os.environ` the check
        cannot fire — `pop` either removes the key or raises — so it is driven with a
        mapping whose `pop` does not, which is what the check is a backstop against: a
        future caller handing it something layered.
        """
        class _KeepsWhatItPops(dict):
            def pop(self, key, *args):
                return self[key]

        with suite_env_guard.isolated_record():
            with self.assertRaisesRegex(RuntimeError, "without removing it"):
                suite_env_guard.strip_operator_env(
                    _KeepsWhatItPops({"METDSL_A_KNOB": "1"}))

    def test_the_stripped_or_declared_decision_reports_an_undecided_name(self) -> None:
        """The decision above, driven where the answer is not "nothing".

        On this tree it returns the empty set, so the assertion that consumes it is green
        whatever the decision does — including returning the empty set unconditionally,
        which survived a sweep while it lived as an expression inside that test.
        """
        self.assertEqual(
            suite_env_guard.undecided_environment_names(
                {"METDSL_A_KNOB", "CODEX_HOME"} | set(MUST_BE_INHERITED)), set())
        self.assertEqual(
            suite_env_guard.undecided_environment_names({"GEMINI_CONFIG_DIR"}),
            {"GEMINI_CONFIG_DIR"})
        self.assertEqual(
            suite_env_guard.undecided_environment_names({"METDSL2_NEW_KNOB"}),
            {"METDSL2_NEW_KNOB"},
            "a near-miss prefix is not the prefix")

    def test_driving_the_strip_leaves_no_global_state_behind(self) -> None:
        """`isolated_record` restores the record AND `CONFIGURED`.

        Both are process-global. `CONFIGURED` left up by a sibling means the witness's
        "the hook never ran, or wrote to a different module" check is satisfied by that
        sibling rather than by the hook, depending on execution order.
        """
        before_record = dict(suite_env_guard.STRIPPED_OPERATOR_ENV)
        before_flag = suite_env_guard.CONFIGURED

        # TWO levels, and the outer one is what makes the flag half observable. Under
        # pytest the session's `CONFIGURED` is already True, so comparing it before and
        # after a single block passes whether or not the restore happens — measured, that
        # is exactly how "isolated_record does not restore CONFIGURED" survived a sweep.
        # The outer context puts the session's real value back; inside it the flag is set
        # to a value the inner block must restore and the strip must change.
        with suite_env_guard.isolated_record():
            suite_env_guard.CONFIGURED = False
            # A SENTINEL, for the same reason as the flag: `__enter__` clears the record,
            # so an outer state of `{}` is indistinguishable from a missing restore —
            # measured, dropping the record restore survived a sweep once the DECLINED
            # block was added, because a later `__enter__` cleared the residue the
            # assertion at the end would have seen.
            suite_env_guard.STRIPPED_OPERATOR_ENV["METDSL_SENTINEL"] = "outer"
            with suite_env_guard.isolated_record():
                environ = {"METDSL_A_KNOB": "1", "PATH": "/b"}
                suite_env_guard.strip_operator_env(environ)
                self.assertTrue(suite_env_guard.CONFIGURED)
                self.assertEqual(suite_env_guard.STRIPPED_OPERATOR_ENV,
                                 {"METDSL_A_KNOB": "1"})
            self.assertEqual(suite_env_guard.STRIPPED_OPERATOR_ENV,
                             {"METDSL_SENTINEL": "outer"},
                             "isolated_record did not restore the record")
            self.assertFalse(suite_env_guard.CONFIGURED,
                             "isolated_record did not restore CONFIGURED")
            del suite_env_guard.STRIPPED_OPERATOR_ENV["METDSL_SENTINEL"]
            # DECLINED too. It was left out of the first version and survived a sweep,
            # because nothing in the suite calls `decline_strip` — a latent leak that a
            # future test inside this context would turn into "the operator declined" for
            # a whole session where nobody passed the flag.
            with suite_env_guard.isolated_record():
                suite_env_guard.decline_strip()
                self.assertTrue(suite_env_guard.DECLINED)
            self.assertFalse(suite_env_guard.DECLINED,
                             "isolated_record did not restore DECLINED")
        self.assertEqual(suite_env_guard.STRIPPED_OPERATOR_ENV, before_record)
        self.assertEqual(suite_env_guard.CONFIGURED, before_flag)

    def test_only_a_name_the_guard_strips_counts_as_an_import_time_capture(self) -> None:
        """The rule the import-spy assertion consumes, driven where it can say something.

        On this tree nothing is reported, so the assertion that consumes it is green
        whatever the rule computes — the mutant that made it always empty survived a
        sweep. Both directions matter here: reporting too little is a hole, and reporting
        too much is the false refusal this rule replaced.
        """
        self.assertEqual(
            suite_env_guard.import_reads_that_would_be_stripped(
                {"METDSL_A_KNOB", "CODEX_HOME"}),
            {"METDSL_A_KNOB", "CODEX_HOME"})
        self.assertEqual(
            suite_env_guard.import_reads_that_would_be_stripped(
                set(MUST_BE_INHERITED) | {"_PYTHON_SUBPROCESS_USE_POSIX_SPAWN"}),
            set(),
            "a name the guard never strips cannot be an import-time capture, and "
            "refusing it is the over-refusal this rule was written to remove")

    def test_no_environment_read_during_the_hooks_own_import_caches_a_value(self) -> None:
        """The hook's import must not itself read the operator's environment.

        `strip_operator_env` snapshots the candidate names, imports
        `tools.orchestration_runtime` to learn the prefix, then pops. Anything read during
        that import happens BEFORE the pop, so the value is the operator's and it is kept
        for the whole session with every test still green.

        MEASURED, NOT PARSED, and the change of instrument is the point. Two static
        readers were written for this question and both were wrong, in opposite
        directions, in consecutive review rounds. The first excluded only top-level
        `def`/`class`, so a fallback `def` inside a module-level `try:` was reported as an
        import-time read. The second excluded those NODES entirely, and thereby stopped
        seeing a class body, a decorator argument, a default argument and a class base —
        all of which execute at import — while still reporting a module-level `lambda`
        body, which does not. Replayed over the twelve shapes tabulated below, that reader is
        wrong on FIVE: it misses the class body, the class base, the decorator argument and
        the default argument, and it refuses the lambda. (An earlier version of this
        sentence said "five of eight", which was the size of the probe set used when the
        defect was found, not of the table beside it — no grouping of eight here contains
        five.) The two files it scanned hold 29 class-body assignments, 10 decorators and
        20 lambdas, so neither error was hypothetical. It also scanned 2 files while the import pulls in
        15, so an ordinary module-level constant in any of the other 13 was invisible.

        Deciding "what runs at import" from source is the wrong question to ask a reader.
        Running the import with `os.environ` instrumented answers it exactly, needs no
        grammar, and covers every spelling at once — including a name assembled at runtime
        and a read made by another module on this one's behalf, neither of which any
        static reader can see.
        """
        reads = self._names_read_during_import("tools.orchestration_runtime")
        # ONLY a name the guard would strip can be cached wrongly. The first version
        # subtracted a hand-written allowlist instead and so refused any import-time read
        # at all — including `HOME`, which is in `MUST_BE_INHERITED` and is never stripped,
        # so nothing about it can be cached wrongly. Worse, the message told the reader to
        # declare it in `MUST_BE_INHERITED`, where it already was: an instruction that
        # changes nothing is not a remedy. Asking the guard directly removes the
        # over-refusal AND the allowlist, whose one member was never stripped either.
        offenders = {name: self._last_import_reads[name] for name in
                     suite_env_guard.import_reads_that_would_be_stripped(reads)}
        self.assertEqual(
            offenders, {},
            "the hook's own import read a name the guard strips, so the operator's value "
            "for it was cached before the strip could remove it. The location is recorded "
            "because the import reaches 15 modules and that is the whole question. Move "
            "the read inside a function — a value read at call time is read after the "
            f"strip. {offenders}")

    def test_the_import_spy_sees_every_shape_that_runs_at_import(self) -> None:
        """The instrument's own witness, in BOTH directions.

        A negative assertion is green when its detector is broken, and the two readers
        this replaces were each defeated by a shape their self-test did not contain. So
        the shapes go in explicitly, and the ones that must NOT be reported go in beside
        them.
        """
        # (label, source, the name that must be reported). The expected name is carried
        # rather than derived from the label: deriving it made one row assert a name no
        # probe could ever produce, which is a row that cannot fail for the right reason.
        runs_at_import = (
            ("plain",
             'X = os.environ.get("PROBE_PLAIN")', "PROBE_PLAIN"),
            ("class body",
             'class C:\n    X = os.environ.get("PROBE_CLASS")', "PROBE_CLASS"),
            ("class base",
             'def mk(v): return object\n'
             'class C(mk(os.environ.get("PROBE_BASE"))): pass', "PROBE_BASE"),
            ("decorator argument",
             'def dec(v):\n    def w(f): return f\n    return w\n'
             '@dec(os.environ.get("PROBE_DECORATOR"))\n'
             'def f(): pass', "PROBE_DECORATOR"),
            ("default argument",
             'def f(x=os.environ.get("PROBE_DEFAULT")): pass', "PROBE_DEFAULT"),
            ("subscript",
             'X = os.environ["HOME"] if "HOME" in os.environ else None\n'
             'Y = os.environ.get("PROBE_SUBSCRIPT")', "PROBE_SUBSCRIPT"),
            ("runtime-computed name",
             'N = "PROBE_" + "COMPUTED"\nX = os.environ.get(N)', "PROBE_COMPUTED"),
            ("read through another module",
             'X = __import__("os").environ.get("PROBE_INDIRECT")', "PROBE_INDIRECT"),
        )
        for label, body, expected in runs_at_import:
            with self.subTest(shape=label):
                self.assertIn(expected, self._names_read_during_import_of_source(body))
                # AND the location must be the probe file. Without this the frame walk is
                # unexercised — every probe reported `<frozen _collections_abc>`, which is
                # exactly what the walk was added to stop printing.
                self.assertTrue(
                    self._last_import_reads[expected].endswith(
                        "metdsl_probe_module.py:" + str(body.count("\n") + 2))
                    or "metdsl_probe_module.py:" in self._last_import_reads[expected],
                    f"the spy did not attribute the read to the probe file: "
                    f"{self._last_import_reads[expected]}")

        never_runs_at_import = {
            "function body": 'def f():\n    return os.environ.get("PROBE_FUNC")',
            "method body": ('class C:\n    def m(self):\n'
                            '        return os.environ.get("PROBE_METHOD")'),
            "lambda body": 'F = lambda: os.environ.get("PROBE_LAMBDA")',
            "def in a module-level try": ('try:\n    import zzz_no_such\n'
                                          'except ImportError:\n    def f():\n'
                                          '        return os.environ.get("PROBE_TRY")'),
        }
        for label, body in never_runs_at_import.items():
            with self.subTest(shape=label):
                seen = self._names_read_during_import_of_source(body)
                self.assertEqual(
                    {n for n in seen if n.startswith("PROBE_")}, set(),
                    f"{label} does not run at import and must not be reported")

    def _names_read_during_import_of_source(self, body: str) -> set[str]:
        """Run the spy over a synthetic module built from `body`."""
        with tempfile.TemporaryDirectory() as td:
            module = Path(td) / "metdsl_probe_module.py"
            module.write_text("import os\n" + body + "\n", encoding="utf-8")
            return self._names_read_during_import(
                "metdsl_probe_module", extra_path=td)

    def _names_read_during_import(self, module: str,
                                  extra_path: str | None = None) -> set[str]:
        """Every environment name read while `module` is imported, in a fresh process.

        `os.environ` is replaced with a recording mapping BEFORE the import. A subprocess
        because the modules under test are already imported in this one, and an import
        that does not happen reads nothing.
        """
        with tempfile.TemporaryDirectory() as td:
            spy = Path(td) / "metdsl_import_spy.py"
            spy.write_text(_IMPORT_SPY_SOURCE, encoding="utf-8")
            argv = [sys.executable, str(spy), module]
            if extra_path:
                argv.extend([extra_path, extra_path])   # sys.path entry, and the walk root
            done = _run(argv, {})
            self.assertEqual(done.returncode, 0,
                             f"the import spy failed:\n{done.stdout}\n{done.stderr}")
            rows = json.loads(done.stdout.strip().splitlines()[-1])
            self._last_import_reads = {n: f"{p}:{ln}" for n, p, ln in rows}
            return set(self._last_import_reads)

    def test_every_test_this_change_cites_by_name_exists(self) -> None:
        """A citation of a test that no longer exists, three times on one branch.

        Round 2 deleted a test for being true by construction and two records still named
        it two commits later. Round 3 replaced the module-scope-read detector and the
        commit that did it left the OLD test's name in `suite_env_guard`'s most
        load-bearing sentence — the one telling a maintainer what stands between the
        snapshot and the operator's cached value. Both were caught by review, in
        consecutive rounds, which is the definition of a discipline that has stopped
        working; `.claude/skills/atmofab-enforcement-change` rule 3-a says to couple the
        documents to the rule with a check at that point, and this is the check.

        Scoped to the files this change owns, and to CODE — a document may legitimately
        name a deleted test as deleted, and `TODO.md` does. A code comment pointing at a
        test is telling the reader where to look, so it has to lead somewhere.
        """
        cited_names = set()
        for rel in _CITATION_SOURCES:
            text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
            cited_names.update(
                (name, rel) for name in re.findall(r"`(test_[A-Za-z0-9_]+)`", text))
        self.assertTrue(cited_names, "no citations found — the reader is broken")

        defined = set()
        for path in (_REPO_ROOT / "tools" / "tests").glob("test_*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name.startswith("test_")):
                    defined.add(node.name)
        self.assertIn("test_every_test_this_change_cites_by_name_exists", defined,
                      "the definition reader is broken — it cannot see this test")

        # A citation is DANGLING only if the name is not a test, not a test module, and
        # not part of this tree's vocabulary. That last clause is what stops the check
        # refusing legitimate work: `test_id`, `test_profile_version`, `test_predicates`
        # and their kin are spec/IR FIELD names, backticked in 27-42 files each, and the
        # first version of this ratchet reported them as tests that do not exist — with a
        # message telling the maintainer to rename something that has no other name.
        # Measured: on this rule the three field names pass, `test_orchestration_runtime`
        # (a module) passes, and the two test names this branch actually deleted are
        # still reported. The cost is a false NEGATIVE for a deleted test whose name
        # survives elsewhere in the tree; that is the safe direction for a ratchet.
        stems = {p.stem for p in (_REPO_ROOT / "tools" / "tests").glob("test_*.py")}
        vocabulary = collections.Counter()
        for path in _REPO_ROOT.rglob("*"):
            if path.suffix not in (".py", ".md", ".yaml", ".json", ".ini"):
                continue
            rel = str(path.relative_to(_REPO_ROOT))
            if rel.startswith((".git/", "workspace")) or rel in _CITATION_SOURCES:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            vocabulary.update(set(re.findall(r"\btest_[A-Za-z0-9_]+\b", text)))

        dangling = sorted(
            (n, where) for n, where in cited_names
            if n not in defined and n not in stems and vocabulary[n] < 2)
        self.assertEqual(
            dangling, [],
            "a comment names a test that does not exist and is not part of this tree's "
            "vocabulary, so it points a maintainer at nothing. Rename the citation or "
            f"drop it: {dangling}")

    def test_the_strip_is_reported_and_can_be_declined(self) -> None:
        """Silence here is a check recorded as run and not run — so both are witnessed.

        `METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT=1` was 84 failures and 482s of real
        probing on `165c26f`; with the strip it is 1242 passed in 49s and nothing probed.
        An operator who set it deliberately has to be able to see that, and to say they
        meant it. Neither the header nor the flag had a witness; both mutants survived.
        """
        poison = {"METDSL_ORCHESTRATION_ID": "orch_header_witness"}
        node = f"tools/tests/{_SUBJECT_MODULE.rsplit('.', 1)[1]}.py" \
               f"::{_SUBJECT_CLASS}::{_SUBJECT_TEST}"

        # BOTH verbosities, and `-q` is the one that matters: it is the only form this
        # repository documents, and it suppresses the session preamble the header prints
        # in. Asserting only the verbose form is how the disclosure came to be invisible
        # in every invocation anyone is actually told to use.
        # BOTH PRODUCERS, by COUNT. There are two — `pytest_report_header` (session
        # preamble, suppressed by `-q`) and `pytest_terminal_summary` (survives `-q`) —
        # and asserting only that the line APPEARS is satisfied by the summary alone at
        # every verbosity, which is how deleting the header survived a sweep. The header
        # is the one that survives `-p no:terminal`, so it is not redundant.
        for extra, expected in (([], 2), (["-q"], 1)):
            with self.subTest(verbosity=extra or ["default"]):
                reported = _run([sys.executable, "-m", "pytest", node] + extra, poison)
                seen = reported.stdout.count("stripped the operator's environment")
                self.assertEqual(
                    seen, expected,
                    f"with {extra or 'no flag'} the strip was disclosed {seen} times, "
                    f"expected {expected} (header + summary at default verbosity, summary "
                    f"alone under -q):\n{reported.stdout[:2000]}")
                self.assertIn("METDSL_ORCHESTRATION_ID", reported.stdout)
                self.assertIn("--keep-operator-env", reported.stdout)
        # The control: with nothing to strip there is no line to print.
        quiet = _run([sys.executable, "-m", "pytest", node, "-q"], {})
        self.assertNotIn("stripped the operator's environment", quiet.stdout)

        # And the flag really declines the strip: the subject test asserts the OUTSIDE-a-run
        # branch, so keeping the name is what makes it fail.
        declined = _run(
            [sys.executable, "-m", "pytest", node, "--keep-operator-env"], poison)
        self.assertNotEqual(
            declined.returncode, 0,
            "--keep-operator-env did not leave the operator's value in place:\n"
            + declined.stdout)
        self.assertIn("--keep-operator-env", declined.stdout,
                      "the run did not say the environment was left alone")
        declined_quiet = _run(
            [sys.executable, "-m", "pytest", node, "--keep-operator-env", "-q"], poison)
        self.assertIn("NOT\nstripped".replace("\n", " "), declined_quiet.stdout,
                      "declining was not disclosed under -q")

        # THE FLAG MUST NOT FAIL A CLEAN HOST. It failed one test with no knob set at all
        # — `1 failed, 5293 passed, 1 skipped` — because the hook returned before setting
        # CONFIGURED and the witness reads that flag to detect "the hook never ran". An
        # escape hatch that manufactures a failure belonging to nothing is this branch's
        # own subject, so it is driven here rather than trusted.
        # ONE node, not this file: running the whole file here selects this test too, and
        # the subprocess spawns another one. Measured as a 300s timeout the first time.
        own_node = (f"tools/tests/{Path(__file__).name}::{type(self).__name__}"
                    "::test_no_ambient_operator_name_survives_into_a_running_test")
        clean_declined = _run(
            [sys.executable, "-m", "pytest", own_node, "--keep-operator-env", "-q"], {})
        self.assertEqual(
            clean_declined.returncode, 0,
            "--keep-operator-env fails on a host with nothing set:\n"
            + clean_declined.stdout)

    def test_a_bare_pytest_at_the_repository_root_loads_the_guard(self) -> None:
        """`testpaths` is what makes the bare invocation load conftest as an initial one.

        Without it, `python3 -m pytest` with no path argument does not load
        `tools/tests/conftest.py` early enough to register `--keep-operator-env` (a hard
        argparse error) or to print the disclosure, while the strip still happens during
        collection — round 1's defect in a second form. Deleting the `testpaths` lines
        survived a sweep, so the fix had no witness of its own.

        Driven with `--co -q` and a `-k` that matches nothing: the option has to be
        ACCEPTED, and collection has to reach this file, without paying for a suite run.
        """
        accepted = _run(
            [sys.executable, "-m", "pytest", "--keep-operator-env", "--co", "-q",
             "-k", "metdsl_no_such_test_name"], {})
        self.assertEqual(
            accepted.returncode, 5,          # 5 = collected, nothing selected
            "a bare `pytest` did not accept --keep-operator-env, so tools/tests/"
            "conftest.py was not loaded as an initial conftest — check `testpaths` in "
            f"pytest.ini:\n{accepted.stdout}\n{accepted.stderr}")

        # And it must collect the WHOLE suite. `assertIn("tests collected", ...)` was the
        # first version and pinned nothing: the real line is "no tests collected", which
        # contains that substring whether 5303 items were collected or 17. A `testpaths`
        # narrowed to one file therefore survived, while README says a bare `pytest` runs
        # the suite. Compared against the explicit form rather than a constant, so adding
        # a test does not make this fail.
        explicit = _run(
            [sys.executable, "-m", "pytest", "tools/tests/", "--co", "-q",
             "-k", "metdsl_no_such_test_name"], {})
        deselected = re.search(r"(\d+) deselected", accepted.stdout)
        expected = re.search(r"(\d+) deselected", explicit.stdout)
        self.assertTrue(deselected and expected, accepted.stdout + explicit.stdout)
        self.assertEqual(
            deselected.group(1), expected.group(1),
            "a bare `pytest` collected a different set from `pytest tools/tests/` — "
            "`testpaths` is not pointing at the whole suite")

    def test_a_poisoned_environment_is_neutralized_end_to_end(self) -> None:
        """Through real processes, with the control that must fail.

        Nothing here is mocked: the guard is a pytest hook, so the only way to observe it
        is to start pytest. `unittest` loads no conftest, which makes it the control
        runner — the same code, the same poison, no guard.
        """
        poison = {"METDSL_ORCHESTRATION_ID": "orch_witness"}
        node = f"tools/tests/{_SUBJECT_MODULE.rsplit('.', 1)[1]}.py" \
               f"::{_SUBJECT_CLASS}::{_SUBJECT_TEST}"
        unittest_target = f"{_SUBJECT_MODULE}.{_SUBJECT_CLASS}.{_SUBJECT_TEST}"

        guarded = _run([sys.executable, "-m", "pytest", node, "-q"], poison)
        self.assertEqual(guarded.returncode, 0,
                         f"the guard did not neutralize the poison:\n{guarded.stdout}")

        # CONTROL 1 — the poison is real. Without the conftest the same test fails.
        control = _run([sys.executable, "-m", "unittest", unittest_target], {})
        self.assertEqual(control.returncode, 0,
                         f"the control runner cannot run the subject at all:\n"
                         f"{control.stderr}")
        poisoned = _run([sys.executable, "-m", "unittest", unittest_target], poison)
        self.assertNotEqual(
            poisoned.returncode, 0,
            "the control did not fail, so the guarded run proves nothing — the subject "
            "test no longer reads the ambient environment and this witness needs a new "
            f"subject:\n{poisoned.stderr}")


if __name__ == "__main__":
    unittest.main()
