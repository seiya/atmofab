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
from pathlib import Path

import yaml

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


_STATUS_UNPADDED = ("written as the supplied literal without the trailing blanks that pad it to "
                    "the fixed width")
_STATUS_NA_CHECK = "{ id = 'status_na', status = 'na  ' }"
_STATUS_NA_REF = "`checks.status_na.status`"
_STATUS_NA_PER_CASE = "`per_case_status: { l0_metric_leaf_pass: na }`"
_METRIC_TEST = re.compile(r"^- `test_id`: `l0_metric_leaf_pass`\n(?:(?!- `test_id`).*\n?)*", re.M)


def harness_status_literal_unstated(entries: list[dict], root: Path) -> list[str]:
    """Every spec publishing `<spec_id>__write_diagnostics(` whose writer item does not state
    that a per-case check status is written without its padding, whose self-test paragraph
    (the one opening "The self-test `") does not supply the padded check `status_na`, or whose
    `tests.md` `l0_metric_leaf_pass` block does not reference `checks.status_na.status`, or
    whose `tests.md` does not state, once and outside that block, the `per_case_status` the IR
    declares for `status_na` (which the Compile leaves transcribe).

    The padded `status_na` is what puts a `na` into the harness's own `diagnostics.json`,
    where the `post_execute` status-vocabulary gate reads it (issue #437); without it a
    writer that keeps the padding is caught only at a consuming node. This pins the
    statements' presence and placement, not what they mean."""
    out: list[str] = []
    for e in entries:
        sid = e["spec_id"]
        cs = (root / e["controlled_spec_path"]).read_text(encoding="utf-8")
        writer = [ln for ln in cs.split("\n")
                  if ln.startswith(f"- `{sid}__write_diagnostics(")]
        if not writer:
            continue
        if len(writer) != 1 or _STATUS_UNPADDED not in writer[0]:
            out.append(f"{sid}: the __write_diagnostics item does not state the unpadded "
                       "status literal")
        paras = [p for p in cs.split("\n") if p.startswith(_SELF_TEST_PARAGRAPH)]
        if len(paras) != 1 or _STATUS_NA_CHECK not in paras[0]:
            out.append(f"{sid}: the self-test paragraph does not supply the padded check "
                       "status_na")
        tp = e.get("tests_path")
        tests = (root / tp).read_text(encoding="utf-8") if tp else ""
        block = _METRIC_TEST.search(tests)
        if not block or _STATUS_NA_REF not in block.group(0):
            out.append(f"{sid}: tests.md l0_metric_leaf_pass does not reference "
                       "checks.status_na.status")
        if tests.count(_STATUS_NA_PER_CASE) != 1 or (block and _STATUS_NA_PER_CASE
                                                     in block.group(0)):
            out.append(f"{sid}: tests.md §5 does not state the per_case_status of status_na once")
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


    def test_every_harness_states_the_status_literal_and_supplies_a_padded_na(self) -> None:
        entries = self._entries()
        # the rule reads a __write_diagnostics item in each harness spec (not vacuous)
        read = [e for e in entries if any(
            ln.startswith(f"- `{e['spec_id']}__write_diagnostics(") for ln in
            (REPO / e["controlled_spec_path"]).read_text(encoding="utf-8").split("\n"))]
        self.assertGreaterEqual(len(read), 3)
        self.assertEqual(harness_status_literal_unstated(entries, REPO), [])

    def test_the_status_literal_rule_is_driven_both_ways(self) -> None:
        """Synthetic: the three statements pass; each missing or misplaced one is reported;
        a spec without a __write_diagnostics item is not read."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            entry = {"spec_id": "s", "controlled_spec_path": "s/controlled_spec.md",
                     "tests_path": "s/tests.md"}
            writer = f"- `s__write_diagnostics(results, n)` — each status is {_STATUS_UNPADDED}."
            para = f"{_SELF_TEST_PARAGRAPH}r` supplies {_STATUS_NA_CHECK} in one case."
            cs = f"# spec\n{writer}\n{para}\n"
            block = (f"- `test_id`: `l0_metric_leaf_pass`\n  - `ref`: {_STATUS_NA_REF}, "
                     "`op`: `eq`\n")
            other = "- `test_id`: `l0_perf_derived_pass`\n  - `judgment`: residual.\n"
            tests = other + f"- `status_na` carries {_STATUS_NA_PER_CASE}.\n" + block

            def run(c: str, t: str) -> list[str]:
                (root / "s/controlled_spec.md").write_text(c)
                (root / "s/tests.md").write_text(t)
                return harness_status_literal_unstated([entry], root)

            self.assertEqual(run(cs, tests), [])
            self.assertIn("unpadded status literal",
                          run(cs.replace(_STATUS_UNPADDED, "x"), tests)[0])
            # the statement sits outside the writer item
            moved = f"# spec\n{writer.replace(_STATUS_UNPADDED, 'x')}\n{_STATUS_UNPADDED}\n{para}\n"
            self.assertIn("unpadded status literal", run(moved, tests)[0])
            self.assertIn("padded check status_na",
                          run(cs.replace(_STATUS_NA_CHECK, "x"), tests)[0])
            # the padded check supplied outside the self-test paragraph
            self.assertIn("padded check status_na",
                          run(cs.replace(_STATUS_NA_CHECK, "x") + _STATUS_NA_CHECK + "\n",
                              tests)[0])
            # the unpadded spelling of the check is not the padded one
            self.assertIn("padded check status_na",
                          run(cs.replace("'na  '", "'na'"), tests)[0])
            self.assertIn("does not reference", run(cs, other)[0])
            # the reference under another test is not the metric test's
            self.assertIn("does not reference",
                          run(cs, block.replace(_STATUS_NA_REF, "x") + other.rstrip("\n")
                              + f" {_STATUS_NA_REF}\n")[0])
            self.assertIn("per_case_status",
                          run(cs, tests.replace(_STATUS_NA_PER_CASE, "x"))[0])
            # stated twice, or only inside the metric test's block, is not the §5 statement
            self.assertIn("per_case_status",
                          run(cs, tests + _STATUS_NA_PER_CASE + "\n")[0])
            self.assertIn("per_case_status",
                          run(cs, tests.replace(_STATUS_NA_PER_CASE, "x")
                              + f"  - `judgment`: {_STATUS_NA_PER_CASE}\n")[0])
            self.assertEqual(run(f"# spec\n{para}\n", "y\n"), [])



if __name__ == "__main__":
    unittest.main()
