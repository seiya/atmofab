#!/usr/bin/env python3
"""Every file a test reads out of the repository must be tracked by git.

The defect this closes: `test_real_full_fidelity_predicate_set_is_not_degenerate` pinned its
calibration input at `<repo>/workspace/ir/.../spec.ir.yaml` and skipped when it was absent.
`workspace/` and `workspace_*/` are the operator's execution workspace — gitignored, pruned at
the operator's discretion, and absent from every fresh clone — so the test could never run there.
It skipped silently for weeks while its own comment declared it the real-shape calibration line.

A test input living in space the repository does not own is the class; that one test was the
instance. The rule enforced here is therefore not "no test may name `workspace`" — a denylist of
tokens grows a hole every time a new generated tree appears — but the property that actually
matters: a path a test builds from the repository root must be something a fresh clone has.
`git ls-files` is the definition, so a new gitignored tree is covered the day it is created.

Scope and its limits. This reads the test sources with `ast` and follows only paths anchored at
`Path(__file__).resolve().parents[N]` (N >= 2, i.e. escaping `tools/tests/` to the repo root),
including through a name bound to such an expression. Paths built under a `TemporaryDirectory`
are not repository reads and are not collected. A segment that is not a string literal truncates
the chain, and the literal prefix up to it is checked — so `REPO_ROOT / "workspace" / name` is
still caught by its `workspace` prefix. A test that reaches the repository some other way (an
os.environ path, a cwd-relative `Path("tools/tests/data/...")`) is out of this checker's reach.
"""

from __future__ import annotations

import ast
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tools" / "tests"

# `Path(__file__).resolve().parents[N]`. N >= 2 escapes `tools/tests/` and lands at or above the
# repository root; parents[0]/parents[1] stay inside the test tree, where everything is tracked.
_ANCHOR_RE = re.compile(r"^Path\(__file__\)\.resolve\(\)\.parents\[(\d+)\]$")


def _is_anchor_expr(node: ast.AST) -> bool:
    m = _ANCHOR_RE.match(ast.unparse(node))
    return bool(m) and int(m.group(1)) >= 2


_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _bindings_in_scope(scope: ast.AST) -> tuple[set[str], set[str]]:
    """(names bound to an anchor, names bound to anything else) directly in this scope.

    Nested function/class bodies are their own scopes and are not descended into. The second set
    matters as much as the first: `repo_root` is bound to a repo-root anchor in one test and to a
    TemporaryDirectory in the next, and a name with any non-anchor binding must not be trusted —
    that ambiguity is what a module-wide name set gets wrong, and it produced 382 false hits.
    """
    anchors: set[str] = set()
    other: set[str] = set()

    def targets_of(node: ast.AST) -> list[ast.AST]:
        if isinstance(node, ast.Assign):
            return list(node.targets)
        if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            return [node.target]
        if isinstance(node, (ast.For, ast.AsyncFor)):
            return [node.target]
        if isinstance(node, (ast.With, ast.AsyncWith)):
            return [i.optional_vars for i in node.items if i.optional_vars is not None]
        if isinstance(node, ast.NamedExpr):
            return [node.target]
        return []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_NODES):
                continue
            value = getattr(child, "value", None)
            is_anchor = (isinstance(child, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
                         and value is not None and _is_anchor_expr(value))
            for target in targets_of(child):
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        (anchors if is_anchor else other).add(name.id)
            visit(child)

    visit(scope)
    return anchors, other


def _anchor_names(tree: ast.AST) -> set[str]:
    """Anchor names visible at module scope (the entry point the unit tests below exercise)."""
    anchors, other = _bindings_in_scope(tree)
    return anchors - other


def _resolve(node: ast.AST, anchors: set[str]) -> str | None:
    """Repo-root-relative path of a `/`-chain rooted at an anchor, or None if not so rooted.

    A non-literal segment truncates the chain rather than discarding it, so the known prefix is
    still checked. Returns "" for the bare anchor (the repo root itself, always fine).
    """
    if isinstance(node, ast.Name) and node.id in anchors:
        return ""
    if _is_anchor_expr(node):
        return ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _resolve(node.left, anchors)
        if left is None:
            return None
        if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            return f"{left}/{node.right.value}".strip("/")
        return left  # truncate at the first non-literal segment, keep the prefix
    return None


def _collect_repo_paths(path: Path) -> set[str]:
    """Repo-root-relative paths built anywhere in one module, resolved scope by scope."""
    found: set[str] = set()

    def walk_scope(scope: ast.AST, inherited: set[str]) -> None:
        local_anchors, local_other = _bindings_in_scope(scope)
        visible = ((inherited - local_other) | local_anchors) - (local_anchors & local_other)

        def visit(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, _SCOPE_NODES):
                    walk_scope(child, visible)
                    continue
                if isinstance(child, ast.BinOp) and isinstance(child.op, ast.Div):
                    rel = _resolve(child, visible)
                    if rel:
                        found.add(rel)
                visit(child)

        visit(scope)

    walk_scope(ast.parse(path.read_text(encoding="utf-8")), set())
    return found


def _tracked() -> tuple[set[str], set[str]]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True,
                         capture_output=True, text=True).stdout
    files = {p for p in out.split("\0") if p}
    dirs: set[str] = set()
    for f in files:
        parts = f.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    return files, dirs


class TestInputsAreTrackedTests(unittest.TestCase):
    def test_no_test_reads_untracked_paths(self) -> None:
        files, dirs = _tracked()
        modules = sorted(TESTS_DIR.rglob("*.py"))
        self.assertTrue(modules, "no test modules found — the scan target is wrong")
        offenders: list[str] = []
        for module in modules:
            for rel in sorted(_collect_repo_paths(module)):
                if rel not in files and rel not in dirs:
                    offenders.append(f"{module.relative_to(REPO_ROOT)} -> {rel}")
        self.assertEqual(offenders, [], (
            "these tests build a repository path that git does not track, so a fresh clone "
            "cannot run them. Capture the input into tools/tests/data/ and point the test "
            f"there: {offenders}"))

    def test_scan_finds_the_known_repo_reads(self) -> None:
        # Without this the checker could silently resolve nothing and pass vacuously — the same
        # failure mode as the skip it exists to prevent. These are real repo reads in the suite.
        expected = {
            "tools/tests/test_cli_reference_sync.py": "docs/CLI_REFERENCE.md",
            "tools/tests/test_validate_pipeline_semantics.py":
                "tools/tests/data/real_ir/shallow_water2d_20260718_003.spec.ir.yaml",
            "tools/tests/llm_samples.py": "docs/examples",
        }
        for rel_module, expected_path in expected.items():
            found = _collect_repo_paths(REPO_ROOT / rel_module)
            self.assertIn(expected_path, found, f"{rel_module}: got {sorted(found)}")

    def test_bare_string_literal_is_not_treated_as_a_repo_read(self) -> None:
        # `test_orchestration_runtime.py` feeds "workspace_20260303/ir/x/spec.ir.yaml" to a path
        # classifier as data. It is not a filesystem read and must not be flagged; the checker
        # keys on the `/`-chain rooted at a repo-root anchor, not on the substring.
        tree = ast.parse('paths = ["workspace_20260303/ir/x/spec.ir.yaml", "workspace/a"]\n')
        self.assertEqual(_anchor_names(tree), set())
        self.assertEqual(
            {r for n in ast.walk(tree) if isinstance(n, ast.BinOp)
             for r in [_resolve(n, set())] if r},
            set())

    def test_checker_flags_an_anchored_untracked_path(self) -> None:
        files, dirs = _tracked()
        src = ('from pathlib import Path\n'
               'ROOT = Path(__file__).resolve().parents[2]\n'
               'IR = ROOT / "workspace" / "ir" / "x" / "spec.ir.yaml"\n'
               'OTHER = Path(__file__).resolve().parents[2] / "workspace_20260719"\n')
        tree = ast.parse(src)
        anchors = _anchor_names(tree)
        self.assertEqual(anchors, {"ROOT"})
        found = {r for n in ast.walk(tree) if isinstance(n, ast.BinOp)
                 for r in [_resolve(n, anchors)] if r}
        self.assertIn("workspace/ir/x/spec.ir.yaml", found)
        self.assertIn("workspace_20260719", found)
        self.assertTrue([p for p in found if p not in files and p not in dirs],
                        "the guard would not have rejected the original defect")

    def test_non_literal_segment_keeps_the_checkable_prefix(self) -> None:
        tree = ast.parse('from pathlib import Path\n'
                         'ROOT = Path(__file__).resolve().parents[2]\n'
                         'P = ROOT / "workspace" / name / "spec.ir.yaml"\n')
        anchors = _anchor_names(tree)
        found = {r for n in ast.walk(tree) if isinstance(n, ast.BinOp)
                 for r in [_resolve(n, anchors)] if r}
        self.assertIn("workspace", found)


if __name__ == "__main__":
    unittest.main()
