#!/usr/bin/env python3
"""The version fields one spec states in three places agree (issue #324, R4-d PR-2).

A spec's `spec_version` is written in `controlled_spec.md` §0, in `tests.md` §0
(`spec_ref.spec_version`) and in `spec/registry/spec_catalog.yaml`; its `test_profile_version`
in `tests.md` §0 and in the `controlled_spec.md` tests-reference sentence ("with
`test_profile_version` of `X`", `docs/CONTROLLED_SPEC.md`). The Compile leaves are handed both
files, so a bump that reaches one and not the other hands them a contradiction. R4-d PR-2 bumped
fifteen `tests.md` and first left all fifteen tests-reference sentences behind.
"""

from __future__ import annotations

import re
import unittest
from unittest import mock
from pathlib import Path

import yaml

from tools.validate_pipeline_semantics import (
    TEST_HISTORY_LINE,
    _tests_md_definition_blocks,
    _parse_test_expected_outcomes_from_tests_md,
    _parse_test_quantities_from_tests_md,
    _tests_md_reported_lines,
)

REPO = Path(__file__).resolve().parents[2]
_SPEC_VERSION = re.compile(r"^- `spec_version`: `([^`]+)`", re.M)
_REF_SPEC_VERSION = re.compile(r"^- `spec_ref\.spec_version`: `([^`]+)`", re.M)
_TEST_PROFILE_VERSION = re.compile(r"^- `test_profile_version`: `([^`]+)`", re.M)
_TESTS_REFERENCE = re.compile(r"with `test_profile_version` of `([^`]+)`")


def version_disagreements(entries: list[dict], root: Path) -> list[str]:
    """Every disagreement among the version fields of the catalog `entries`, read under
    `root`. A field a file does not carry is not compared (a `profile` has no tests.md)."""
    out: list[str] = []
    for e in entries:
        sid = e["spec_id"]
        cs = (root / e["controlled_spec_path"]).read_text(encoding="utf-8")
        found = _SPEC_VERSION.findall(cs)
        if found != [e["spec_version"]]:
            out.append(f"{sid}: controlled_spec.md spec_version {found} != catalog "
                       f"{e['spec_version']!r}")
        tp = e.get("tests_path")
        if not tp or not (root / tp).is_file():
            continue
        tests = (root / tp).read_text(encoding="utf-8")
        ref = _REF_SPEC_VERSION.findall(tests)
        if ref != [e["spec_version"]]:
            out.append(f"{sid}: tests.md spec_ref.spec_version {ref} != catalog "
                       f"{e['spec_version']!r}")
        tpv = _TEST_PROFILE_VERSION.findall(tests)
        cited = _TESTS_REFERENCE.findall(cs)
        if cited and cited != tpv:
            out.append(f"{sid}: controlled_spec.md cites test_profile_version {cited}, "
                       f"tests.md declares {tpv}")
    return out


def profile_selection_on_non_adopters(entries: list[dict], root: Path) -> list[str]:
    """Every `tests.md` that names `profile_selection` although its spec adopts no profile.

    The key is declared exactly when the node adopts a profile (the phase_01 schema block,
    issue #358); a node whose `deps.yaml` `dependencies.profiles` is empty declares none, so
    its `tests.md` has no reason to name it — the harness `tests.md` did, and gave the key a
    second, runtime meaning a Compile.verify then graded against."""
    out: list[str] = []
    for e in entries:
        tp, dp = e.get("tests_path"), e.get("deps_path")
        if not tp or not (root / tp).is_file():
            continue
        if "profile_selection" not in (root / tp).read_text(encoding="utf-8"):
            continue
        deps = yaml.safe_load((root / dp).read_text(encoding="utf-8")) if dp else None
        profiles = ((deps or {}).get("dependencies") or {}).get("profiles") or []
        if not profiles:
            out.append(f"{e['spec_id']}: tests.md names profile_selection, but the spec "
                       "adopts no profile")
    return out


_SELF_TEST_PARAGRAPH = "The self-test `"
_WRITE_PERF = "`__write_perf`"
_CELLS_UPDATED_FIXED = ("`steps` and `cells_updated` both equal to the number of cases its "
                        "`__parse_cases` call on the program's argv returned")
_WALLTIME_FLOOR = "or `1.0e-9` when the elapsed time it reads is not positive"
_DISTRIBUTED = "__comm_size()"
_NOT_SUMMED = "is not summed over ranks"
_CELLS_UPDATED_CITED = ("The `steps`, `cells_updated` and `walltime_sec` the self-test passes to "
                        "`__write_perf` are the run totals `controlled_spec.md` §3 fixes for it")
_PERF_TEST = re.compile(r"^- `test_id`: `l0_perf_derived_pass`\n(?:(?!- `test_id`).*\n?)*", re.M)


def harness_perf_values_unfixed(entries: list[dict], root: Path) -> list[str]:
    """Every spec whose self-test paragraph (the one opening "The self-test `") calls
    `__write_perf` but does not carry, once, the sentences fixing `steps` / `cells_updated`
    and the `walltime_sec` floor (and, in a spec that publishes `__comm_size()`, the
    not-summed-over-ranks clause), or whose `tests.md` `l0_perf_derived_pass` block does not
    cite them.

    The residual of that test divides by the throughput, so a value the inputs leave open
    is the leaf's to choose, and zero leaves the residual undefined (issue #394). This pins
    the sentences' presence and placement, not what they mean."""
    out: list[str] = []
    for e in entries:
        cs = (root / e["controlled_spec_path"]).read_text(encoding="utf-8")
        paras = [p for p in cs.split("\n")
                 if p.startswith(_SELF_TEST_PARAGRAPH) and _WRITE_PERF in p]
        if not paras:
            continue
        sid = e["spec_id"]
        if len(paras) != 1:
            out.append(f"{sid}: {len(paras)} self-test paragraphs call __write_perf")
            continue
        para = paras[0]
        for phrase, what in ((_CELLS_UPDATED_FIXED, "steps / cells_updated"),
                             (_WALLTIME_FLOOR, "the walltime_sec floor")):
            if cs.count(phrase) != 1 or phrase not in para:
                out.append(f"{sid}: the self-test paragraph does not state {what} once")
        if _DISTRIBUTED in cs and _NOT_SUMMED not in para:
            out.append(f"{sid}: the distributed self-test does not say the count is not "
                       "summed over ranks")
        tests = (root / e["tests_path"]).read_text(encoding="utf-8")
        block = _PERF_TEST.search(tests)
        if not block or _CELLS_UPDATED_CITED not in block.group(0) \
                or tests.count(_CELLS_UPDATED_CITED) != 1:
            out.append(f"{sid}: tests.md l0_perf_derived_pass does not cite "
                       "controlled_spec.md §3 for cells_updated / walltime_sec")
    return out


#: What a `tests.md` no longer states since issue #582: the judging half and its carriers.
_RETIRED_TEST_TOKENS = ("pass_when", "required_raw_variables", "diagnostics.json",
                        "metrics_basis", "primary_predicate", "verdict.overall", "failed_checks",
                        "`quantity`")
_RETIRED_CHECK_REF = re.compile(r"\bchecks\.\S")
_OPERATION_ID_LINE = re.compile(r"^\s*-\s*`?operation_id`?\s*:\s*`?([A-Za-z0-9_]+)`?")
#: A logical `out` of a procedure that is state a test judges, never the guard result: the
#: saturation adjustment's `converged` (`docs/workflow/phases/phase_01_compile.md`, gate (d)).
_NON_RESULT_FLAGS = frozenset({"converged"})


def _section_51_results(controlled_spec: str) -> dict[str, list[str]]:
    """The published procedures of a Controlled Spec's §5.1 fence -> their logical `out` names
    other than `_NON_RESULT_FLAGS`. Empty when the document has no such fence."""
    tail = controlled_spec.split("### 5.1", 1)
    if len(tail) != 2 or "```yaml" not in tail[1]:
        return {}
    block = yaml.safe_load(tail[1].split("```yaml", 1)[1].split("```", 1)[0]) or {}
    out: dict[str, list[str]] = {}
    for proc in block.get("procedures") or []:
        out[proc["name"]] = [
            a["name"] for a in proc.get("args") or []
            if a.get("intent") == "out" and (a.get("spec") or {}).get("type") == "logical"
            and a["name"] not in _NON_RESULT_FLAGS]
    return out


def flag_rule_findings(sid: str, controlled_spec: str, tests_path: Path) -> list[str]:
    """`docs/TESTS.md` requirement 9 and §Description format, the names the Compile gate requires
    on a `quantities` line, over a physics node's own documents (a harness has no `drive`): a
    `pass` test of a node with no time loop lists the result of its `operation_id`; a `pass` test
    of a time-marching node lists `run.n_step`, and the `__init` result when one is published;
    no test lists the retired `input_guard`. The gate refuses these only after a billed
    `Compile.generate`. This reads names, not what the prose judges."""
    results = _section_51_results(controlled_spec)
    tests = tests_path.read_text(encoding="utf-8")
    parsed = _parse_test_quantities_from_tests_md(tests_path)
    outcomes = _parse_test_expected_outcomes_from_tests_md(tests_path)
    blocks = _tests_md_definition_blocks(tests_path) or {}
    if parsed is None or outcomes is None:
        return [f"{sid}: tests.md is unreadable"]
    looping = any(TEST_HISTORY_LINE.match(line.strip()) for line in tests.splitlines())
    init = next((names for name, names in results.items() if name.endswith("__init")), None)
    out: list[str] = []
    for tid, names in parsed[0].items():
        if "input_guard" in names:
            out.append(f"{sid}: {tid} lists the retired `input_guard`")
        if outcomes[0].get(tid) != "pass":
            continue
        if looping:
            need = ["run.n_step", *(init or [])]
        else:
            operation = next((m.group(1) for m in map(_OPERATION_ID_LINE.match, blocks.get(tid, []))
                              if m), None)
            need = results.get(operation or "", ["<no §5.1 procedure named by operation_id>"])
        out.extend(f"{sid}: pass test {tid} does not list {n!r}" for n in need if n not in names)
    return out


def suites_unmigrated(entries: list[dict], root: Path) -> list[str]:
    """Every `tests.md` naming a retired token, or that the Compile gate refuses: a test with no
    `quantities` line, with two, or with a non-name entry; a `reported` line anywhere (issue #589
    retired the class); and a suite whose CATALOG `spec_kind` is `problem` with no `history:` line
    (a problem runs a time loop, issue #589 gate (m); a component's line — rk4's — is admitted and
    pinned by gate (m) alone). The gate refuses those only after the leaf has spent its cost. This
    pins the statements' presence, not what they mean."""
    out: list[str] = []
    for e in entries:
        tp = e.get("tests_path")
        if not tp:
            continue
        sid = e["spec_id"]
        tests = (root / tp).read_text(encoding="utf-8")
        for token in _RETIRED_TEST_TOKENS:
            if token in tests:
                out.append(f"{sid}: tests.md names the retired {token}")
        if _RETIRED_CHECK_REF.search(tests):
            out.append(f"{sid}: tests.md names the retired checks.<id>")
        parsed = _parse_test_quantities_from_tests_md(root / tp)
        stated, problems = parsed if parsed is not None else ({}, [])
        if not stated and not problems:
            out.append(f"{sid}: tests.md defines no test")
        out.extend(f"{sid}: {problem}" for problem in problems)
        outcomes = _parse_test_expected_outcomes_from_tests_md(root / tp)
        out.extend(f"{sid}: {problem}" for problem in (outcomes[1] if outcomes else
                                                       ["tests.md is unreadable"]))
        out.extend(f"{sid}: tests.md line {n} is a retired `reported:` line"
                   for n, _line in _tests_md_reported_lines(root / tp) or [])
        if e.get("spec_kind") == "problem" and not any(
                TEST_HISTORY_LINE.match(line.strip()) for line in tests.splitlines()):
            out.append(f"{sid}: a problem's tests.md carries no `history:` line")
        if e.get("spec_kind") in ("problem", "component") and e.get("controlled_spec_path"):
            out.extend(flag_rule_findings(
                sid, (root / e["controlled_spec_path"]).read_text(encoding="utf-8"), root / tp))
    return out


class SpecCorpusVersionTest(unittest.TestCase):
    def _entries(self) -> list[dict]:
        doc = yaml.safe_load((REPO / "spec/registry/spec_catalog.yaml").read_text())
        return doc["specs"]

    def test_the_corpus_agrees(self) -> None:
        entries = self._entries()
        # the tests-reference sentence is read for every physics spec (not vacuous)
        cited = [e for e in entries if _TESTS_REFERENCE.search(
            (REPO / e["controlled_spec_path"]).read_text(encoding="utf-8"))]
        self.assertGreaterEqual(len(cited), 15)
        self.assertEqual(version_disagreements(entries, REPO), [])

    def test_each_field_disagreement_is_reported(self) -> None:
        """Drive the rule on a synthetic copy where each field in turn disagrees."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            cs = ("- `spec_version`: `0.1.1`\n\nThe corresponding `tests.md` is `s/tests.md`, "
                  "with `test_profile_version` of `0.2.0`.\n")
            tests = ("- `test_profile_version`: `0.2.0`\n- `spec_ref.spec_version`: `0.1.1`\n")
            entry = {"spec_id": "s", "spec_version": "0.1.1",
                     "controlled_spec_path": "s/controlled_spec.md", "tests_path": "s/tests.md"}

            def run(c: str, t: str, e: dict) -> list[str]:
                (root / "s/controlled_spec.md").write_text(c)
                (root / "s/tests.md").write_text(t)
                return version_disagreements([e], root)

            self.assertEqual(run(cs, tests, entry), [])
            self.assertIn("cites test_profile_version",
                          run(cs, tests.replace("`0.2.0`", "`0.3.0`"), entry)[0])
            self.assertIn("tests.md spec_ref.spec_version",
                          run(cs, tests.replace("`0.1.1`", "`0.1.2`"), entry)[0])
            self.assertIn("controlled_spec.md spec_version",
                          run(cs.replace("`0.1.1`", "`0.1.2`"), tests, entry)[0])
            self.assertEqual(len(run(cs, tests, {**entry, "spec_version": "0.1.2"})), 2)


    def test_no_tests_md_names_profile_selection_on_a_node_adopting_no_profile(self) -> None:
        self.assertEqual(profile_selection_on_non_adopters(self._entries(), REPO), [])

    def test_the_profile_selection_rule_is_driven_both_ways(self) -> None:
        """Synthetic: the corpus answer is empty, so drive a refusal and an acceptance."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            entry = {"spec_id": "s", "tests_path": "s/tests.md", "deps_path": "s/deps.yaml"}
            (root / "s/tests.md").write_text("- each case carries `inputs.profile_selection`\n")
            (root / "s/deps.yaml").write_text("dependencies:\n  profiles: []\n")
            self.assertIn("adopts no profile",
                          profile_selection_on_non_adopters([entry], root)[0])
            (root / "s/deps.yaml").write_text(
                "dependencies:\n  profiles:\n    - profile_id: p\n")
            self.assertEqual(profile_selection_on_non_adopters([entry], root), [])
            (root / "s/deps.yaml").write_text("dependencies:\n  profiles: []\n")
            (root / "s/tests.md").write_text("- selected by its `case_id`\n")
            self.assertEqual(profile_selection_on_non_adopters([entry], root), [])

    def test_every_harness_fixes_the_self_test_perf_values(self) -> None:
        entries = self._entries()
        # the rule reads a perf paragraph in each harness spec (not vacuous)
        read = [e for e in entries if any(
            p.startswith(_SELF_TEST_PARAGRAPH) and _WRITE_PERF in p for p in
            (REPO / e["controlled_spec_path"]).read_text(encoding="utf-8").split("\n"))]
        self.assertGreaterEqual(len(read), 3)
        self.assertEqual(harness_perf_values_unfixed(entries, REPO), [])

    def test_the_perf_values_rule_is_driven_both_ways(self) -> None:
        """Synthetic: the statements and the citation pass; each missing, misplaced or
        doubled is reported; a spec without a perf paragraph is not read."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            entry = {"spec_id": "s", "controlled_spec_path": "s/controlled_spec.md",
                     "tests_path": "s/tests.md"}
            para = (f"{_SELF_TEST_PARAGRAPH}r` calls {_WRITE_PERF} once, with that elapsed time as `walltime_sec`, "
                    f"{_WALLTIME_FLOOR} (a tick), and with {_CELLS_UPDATED_FIXED}: one step.")
            cs = f"# spec\n{para}\n"
            mpi = para.replace("(a tick),", "(a tick), `__comm_size()` as `mpi_ranks`,")
            block = (f"- `test_id`: `l0_perf_derived_pass`\n  - `judgment`: residual. "
                     f"{_CELLS_UPDATED_CITED}, both positive.\n")
            other = "- `test_id`: `l0_metric_leaf_pass`\n  - `judgment`: fold.\n"
            tests = block + other

            def run(c: str, t: str) -> list[str]:
                (root / "s/controlled_spec.md").write_text(c)
                (root / "s/tests.md").write_text(t)
                return harness_perf_values_unfixed([entry], root)

            self.assertEqual(run(cs, tests), [])
            self.assertIn("steps / cells_updated",
                          run(cs.replace(_CELLS_UPDATED_FIXED, "x"), tests)[0])
            self.assertIn("walltime_sec floor", run(cs.replace(_WALLTIME_FLOOR, "x"), tests)[0])
            # the statement sits in another paragraph than the __write_perf one
            moved = f"# spec\n{para.replace(_CELLS_UPDATED_FIXED, 'x')}\n{_CELLS_UPDATED_FIXED}\n"
            self.assertIn("steps / cells_updated", run(moved, tests)[0])
            self.assertIn("steps / cells_updated",
                          run(cs + _CELLS_UPDATED_FIXED + "\n", tests)[0])
            self.assertIn("not summed over ranks", run(f"# spec\n{mpi}\n", tests)[0])
            # keyed on the spec publishing the operation, not on the paragraph's wording
            self.assertIn("not summed over ranks",
                          run(f"# spec\n- `x__comm_size()`\n{para}\n", tests)[0])
            # another paragraph naming __write_perf is not the self-test's
            self.assertEqual(run(cs + f"A host runner likewise calls {_WRITE_PERF} once.\n",
                                 tests), [])
            self.assertEqual(run(f"# spec\n{mpi} The count {_NOT_SUMMED}.\n", tests), [])
            self.assertIn("does not cite", run(cs, other)[0])
            # the citation under another test is not the perf test's
            self.assertIn("does not cite", run(cs, block.replace(_CELLS_UPDATED_CITED, "x")
                                                   + other + _CELLS_UPDATED_CITED + "\n")[0])
            self.assertEqual(run("no perf paragraph\n", "y\n"), [])


    def test_every_suite_is_migrated(self) -> None:
        entries = self._entries()
        # a tests.md of every kind that has one: the three harnesses and the 17 physics nodes
        self.assertEqual(sum(e.get("spec_kind") == "infrastructure" for e in entries), 3)
        self.assertEqual(sum(bool(e.get("tests_path")) for e in entries), 20)
        self.assertEqual(suites_unmigrated(entries, REPO), [])

    def test_each_physics_node_has_a_transcription(self) -> None:
        """Issue #589 PR-3: `data/drive_ir/pr3_evidence/ir/` holds one hand-written IR per physics
        node — exactly those — and a `test_transcription_compile_static_accepts_<spec_id>` row
        is generated for each, so the rows cover every physics node and nothing else."""
        physics = {e["spec_id"] for e in self._entries()
                   if e.get("spec_kind") in ("problem", "component")}
        self.assertEqual({p.stem for p in _PR3_EVIDENCE.glob("*.yaml")}, physics)
        # each row validates its own node's IR, not the last one a loop variable held: run
        # every row with the validation recorded instead of performed
        read: dict[str, str] = {}
        for name in dir(type(self)):
            if name.startswith(_TRANSCRIPTION_ROW):
                with mock.patch.object(type(self), "_transcription_compile_static_accepts",
                                       lambda _self, spec_id, n=name: read.setdefault(n, spec_id)):
                    getattr(self, name)()
        self.assertEqual({n.removeprefix(_TRANSCRIPTION_ROW): v for n, v in read.items()},
                         {spec_id: spec_id for spec_id in physics})

    def _transcription_compile_static_accepts(self, spec_id: str) -> None:
        """`Compile.static` reads the node's hand-written IR against the real `tests.md` (the
        quantities and outcome lines, the history line, the host's case evaluation). A `tests.md`
        change that moves those reddens this row: update the transcription, or a billed `Compile`
        finds out instead. This says the document can be transcribed, not that a leaf will.

        One row per node rather than one loop over all of them: the validator takes about 1-30 s
        per IR, and a single row of all 17 was the suite's longest (96 s at `-n 24`), which no
        number of workers shortens."""
        import json
        import tempfile

        from tools.validate_pipeline_semantics import validate_compile_ir_dir
        with tempfile.TemporaryDirectory() as tmp:
            text = (_PR3_EVIDENCE / f"{spec_id}.yaml").read_text(encoding="utf-8")
            ir = yaml.safe_load(text)
            ir_dir = Path(tmp)
            (ir_dir / "spec.ir.yaml").write_text(text, encoding="utf-8")
            key = ir["meta"]["node_key"]
            deps = [d["node_key"] for d in (ir.get("dependency") or {}).get("direct_deps") or []]
            (ir_dir / "dependency_graph.json").write_text(json.dumps({
                "node_key": key, "direct_deps": deps, "transitive_deps": [],
                "all_nodes": [{"node_key": k, "topo_level": 0} for k in deps]
                + [{"node_key": key, "topo_level": 1 if deps else 0}],
                "profiles": (ir.get("dependency") or {}).get("profiles") or []}))
            (ir_dir / "ir_meta.json").write_text(json.dumps({
                "attempt_count": 1, "verification_status": "pass",
                "last_fail_reason": None, "debug_mode": False, "context_isolated": True}))
            self.assertEqual(validate_compile_ir_dir(REPO, ir_dir), [])

    def test_each_declared_axis_set_distributes_a_pass_case(self) -> None:
        """Issue #610 (B6, §10 approval (a)): for every bench IR whose `distribute:` line
        declares axes, `distribute_plan` — the one replication decider — distributes at least one
        `pass` case on the minimal partitioning grid (2 along each declared axis) and on each
        launcher profile's certified grid. The population is the bench's case values, which no
        gate holds to `tests.md` §4 (case VALUES are V3's reading): the row says "the spec as the
        bench transcribes it distributes a `pass` case", and skips a node whose line is
        `none`. A lower bound of one, not the census counts, so a case edit does not redden it."""
        from tools.runner_ir import distribute_plan, drive_distribute_axes
        evidence = Path(__file__).parent / "data/drive_ir/pr3_evidence/ir"
        launchers = {}
        for path in sorted((REPO / "spec/targets").glob("*.yaml")):
            execution = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
                "execution") or {}
            if execution.get("ranks"):
                grid = execution.get("process_grid") or [execution["ranks"], 1]
                launchers[path.stem] = (int(grid[0]), int(grid[1]))
        self.assertIn("fortran_cpu_mpi", launchers)
        with_axes = 0
        for path in sorted(evidence.glob("*.yaml")):
            ir = yaml.safe_load(path.read_text(encoding="utf-8"))
            axes = drive_distribute_axes(ir)
            if not axes:
                continue
            with_axes += 1
            passing = {c for t in ir["io_contract"]["tests"]
                       if str(t["expected_outcome"]).strip().lower() == "pass"
                       for c in t["target_cases"]}
            cases = [c for c in ir["case"]["test_case_set"] if c["case_id"] in passing]
            minimal = (2, 2 if len(axes) > 1 else 1)
            for label, grid in (("minimal", minimal), *launchers.items()):
                with self.subTest(spec_id=path.stem, grid=label):
                    self.assertGreaterEqual(
                        sum(distribute_plan(ir, c, grid) for c in cases), 1,
                        f"no `pass` case of {path.stem} is distributed on {grid}: the line's "
                        "min_block (or the cases' extents) leave the target's certification "
                        "vacuous")
        self.assertEqual(with_axes, 10)

    def test_the_flag_rule_is_driven_both_ways(self) -> None:
        """Synthetic: a `pass` test missing its operation's result, a time-marching one missing
        `run.n_step` or the `__init` result, and `input_guard` are each reported; each satisfied
        form, and an `xfail` test without a result, is not; `converged` is no result."""
        import tempfile
        spec = ("### 5.1\n```yaml\nprocedures:\n"
                "- {name: s__op, args: [{name: guard_pass, intent: out, spec: {type: logical}},"
                " {name: converged, intent: out, spec: {type: logical}},"
                " {name: x, intent: out, spec: {type: real}}]}\n```\n")
        loop_spec = ("### 5.1\n```yaml\nprocedures:\n"
                     "- {name: s__step, args: [{name: ok, intent: out, spec: {type: logical}}]}\n"
                     "- {name: s__init, args: [{name: init_ok, intent: out, spec: {type: logical}}]}\n"
                     "```\n")

        def run(spec_text: str, outcome: str, quantities: str, op: str = "s__op",
                history: bool = False) -> list[str]:
            head = "## 3. Execution\nhistory: every = 1 (every)\n" if history else ""
            body = (f"{head}## 6. Test definitions\n- `test_id`: `t_a`\n  - `level`: `L0`\n"
                    f"  - `operation_id`: `{op}`\n  - `expected_outcome`: `{outcome}`\n"
                    f"  - `quantities`: {quantities}\n  - `judgment`: x\n## 7. Pass/fail\n")
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "tests.md"
                path.write_text(body, encoding="utf-8")
                return flag_rule_findings("s", spec_text, path)

        self.assertEqual(run(spec, "pass", "`guard_pass`, `q`"), [])
        self.assertEqual(run(spec, "xfail", "`q`"), [])
        self.assertEqual(run(spec, "pass", "`q`"), ["s: pass test t_a does not list 'guard_pass'"])
        self.assertEqual(run(spec, "pass", "`q`", op="s__other"),
                         ["s: pass test t_a does not list '<no §5.1 procedure named by operation_id>'"])
        self.assertIn("s: t_a lists the retired `input_guard`", run(spec, "xfail", "`input_guard`"))
        self.assertEqual(run(loop_spec, "pass", "`run.n_step`, `init_ok`", "s__step", True), [])
        self.assertEqual(run(loop_spec, "pass", "`init_ok`", "s__step", True),
                         ["s: pass test t_a does not list 'run.n_step'"])
        self.assertEqual(run(loop_spec, "pass", "`run.n_step`", "s__step", True),
                         ["s: pass test t_a does not list 'init_ok'"])

    def test_the_migration_rule_is_driven_both_ways(self) -> None:
        """Synthetic: a migrated suite in either id form passes; a retired token, a missing or a
        doubled line, and a suite with no test are each reported; a spec with no tests.md is not
        read."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = {"spec_id": "s", "spec_kind": "component", "tests_path": "t.md"}

            def run(text: str) -> list[str]:
                (root / "t.md").write_text(text)
                return suites_unmigrated([entry], root)

            outcome = "  - `expected_outcome`: `pass`\n"
            good = (f"- `test_id`: `a`\n{outcome}  - `quantities`: `q`\n"
                    f"- `test_id`: `b`\n{outcome}  - `quantities`: `r`\n")
            self.assertEqual(run(good), [])
            self.assertEqual(run(f"## 6. T\n### 6-1. `a`\n{outcome}- `quantities`: `q`\n"), [])
            self.assertIn("'b' has no `expected_outcome:` line", run(
                f"- `test_id`: `a`\n{outcome}  - `quantities`: `q`\n"
                "- `test_id`: `b`\n  - `quantities`: `r`\n")[0])
            for token in _RETIRED_TEST_TOKENS:
                with self.subTest(token=token):
                    self.assertIn(f"retired {token}", run(good + token + "\n")[0])
            self.assertIn("'b' has no", run(f"- `test_id`: `a`\n{outcome}  - `quantities`: `q`\n"
                                            f"- `test_id`: `b`\n{outcome}  - `judgment`: x\n")[0])
            self.assertIn("'a' has 2", run(f"- `test_id`: `a`\n{outcome}  - `quantities`: `q`\n"
                                           "  - `quantities`: `r`\n")[0])
            self.assertIn("retired checks.<id>", run(good + "checks.cfl.status\n")[0])
            self.assertEqual(run(good + "the host checks.\n"), [])
            for spelling in ("checks.<id>", "checks.Zero.status"):
                self.assertIn("retired checks.<id>", run(good + spelling + "\n")[0])
            self.assertEqual(len(run(f"- `test_id`: `a`\n{outcome}- `test_id`: `b`\n{outcome}")), 2)
            # a `reported:` line is retired (issue #589), once per line, either spelling
            self.assertEqual(len(run(good + "  - `reported`: `x`\nreported: y\n")), 2)
            self.assertIn("retired `reported:` line", run(good + "  - `reported`: `x`\n")[0])
            self.assertEqual(run(good + "  - the operation reported: nothing\n"), [])
            self.assertIn("defines no test", run("nothing\n")[0])
            self.assertEqual(suites_unmigrated([{"spec_id": "p", "spec_kind": "profile"}], root), [])
            # a problem (its CATALOG kind) needs a `history:` line; a component does not, and
            # may carry one
            problem = {**entry, "spec_kind": "problem"}
            (root / "t.md").write_text(good)
            self.assertEqual(suites_unmigrated([problem], root),
                             ["s: a problem's tests.md carries no `history:` line"])
            for line in ("- history: every = 38 (history.every)\n",
                         "history: every = (history.every) a = 1, b = 2\n"):
                (root / "t.md").write_text(good + line)
                self.assertEqual(suites_unmigrated([problem], root), [], line)
                self.assertEqual(run(good + line), [], line)


_PR3_EVIDENCE = Path(__file__).parent / "data/drive_ir/pr3_evidence/ir"

_TRANSCRIPTION_ROW = "test_transcription_compile_static_accepts_"

def _transcription_row(spec_id: str):
    """The row for one node: a function of `spec_id` rather than a lambda in the loop, so the
    row does not close over the loop variable."""
    def row(self) -> None:
        self._transcription_compile_static_accepts(spec_id)
    return row


for _spec_id in sorted(p.stem for p in _PR3_EVIDENCE.glob("*.yaml")):
    setattr(SpecCorpusVersionTest, f"{_TRANSCRIPTION_ROW}{_spec_id}",
            _transcription_row(_spec_id))


if __name__ == "__main__":
    unittest.main()
