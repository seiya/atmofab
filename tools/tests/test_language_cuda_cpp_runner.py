"""The CUDA C++ backend's `runner_render` capability (issue #289, R4-b PR-6).

`render_runner` and `render_checks_header` are pure functions of the IR: these tests pin the
rendered shape, determinism, the render-error matrix AGAINST the Fortran renderer's (the two share
the neutral IR readers, `tools/runner_ir.py`, so an IR one target's renderer accepts is not
refused by the other's for a neutral reason), and the harness signature pin. The rows that run the
CUDA compiler driver — the declared lint rule set over the rendered files, and a smoke that
compiles, links and RUNS the rendered runner against a host-only harness stub and a checks stub
(no kernel, so no device is needed) — are skipped where `nvcc` is not on `PATH`.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.backends.language.cuda_cpp import checks_abi as cpp_checks_abi
from tools.backends.language.cuda_cpp import header as cpp_header
from tools.backends.language.cuda_cpp import runner as cpp_runner
from tools.backends.language.cuda_cpp import signatures as cpp_signatures
from tools.backends.language.cuda_cpp import source as cpp_source
from tools.backends.language.fortran import runner as ft_runner
from tools.host_render import RenderError
from tools.structured_signatures import load_structured_signatures
from tools.tests import test_fortran_runner as ft_tests

HARNESS = "harness_cpp_gpu"
NVCC = shutil.which("nvcc")
GPU_TARGET = {"hardware": {"class": "gpu"}, "execution": {"threads_per_rank": 3}}
CPU_TARGET = {"hardware": {"class": "cpu"}, "execution": {"threads_per_rank": 2}}

# The IR fixtures are the Fortran renderer's, deliberately: the renderers read one IR, and the
# differential rows below compare the two over the same inputs.
_boundary_ir = ft_tests._boundary_ir
_metrics_ir = ft_tests._metrics_ir
_rank34_metrics_ir = ft_tests._rank34_metrics_ir
BOUNDARY_SID = ft_tests.BOUNDARY_SID
RANK_SID = ft_tests.RANK_SID


def _render(ir: dict, sid: str, target: dict = GPU_TARGET) -> str:
    return cpp_runner.render_runner(ir, sid, HARNESS, target=target)


def _harness_struct() -> dict:
    struct, error = load_structured_signatures(cpp_runner._HARNESS_V1_INTERFACE)
    assert error is None, error
    return struct


def _harness_public_api() -> dict:
    struct = _harness_struct()
    return {"module_parameters": struct["module_parameters"],
            "signatures": [{"symbol": sig["name"], "signature": sig}
                           for sig in [*struct["types"], *struct["procedures"]]]}


def _harness_signatures() -> list[dict]:
    return _harness_public_api()["signatures"]


# A host-only harness stub: every §5.1 operation defined in namespace `harness_cpp_gpu_model`,
# enough to link the rendered runner and write the run outputs (JSON with no escaping beyond what
# the fixtures need). Its declarations come from the host-rendered header, as a certified
# harness's do; the four array emitters differ only in rank and are generated.


def _harness_stub() -> str:
    emitters = []
    for rank in range(1, 5):
        emitters.append(textwrap.dedent(f"""\
            std::string harness_cpp_gpu__emit_array_r{rank}(atmofab::View<const dp, {rank}> a) {{
              return nested(a.data, a.extent, {rank}, {rank} - 1, 0);
            }}
            """))
    return textwrap.dedent("""\
        #include "harness_cpp_gpu_model.cuh"

        #include <cstdio>
        #include <fstream>

        namespace harness_cpp_gpu_model {
        namespace {
        std::string q(const std::string& s) {
          std::string out = "\\"";
          for (char c : s) {
            if (c == '"' || c == '\\\\') {
              out += '\\\\';
            }
            out += c;
          }
          return out + "\\"";
        }
        std::string trim(const std::string& s) {
          std::size_t end = s.size();
          while (end > 0 && s[end - 1] == ' ') {
            --end;
          }
          return s.substr(0, end);
        }
        std::string real(dp x) {
          char buf[64];
          std::snprintf(buf, sizeof buf, "%.16e", x);
          return buf;
        }
        // Column-major storage emitted with the LAST index outermost.
        std::string nested(const dp* data, const long* extent, int rank, int dim, long base) {
          long stride = 1;
          for (int k = 0; k < dim; ++k) {
            stride *= extent[k];
          }
          std::string out = "[";
          for (long i = 0; i < extent[dim]; ++i) {
            if (i > 0) {
              out += ", ";
            }
            out += dim == 0 ? real(data[base + i]) : nested(data, extent, rank, dim - 1,
                                                          base + i * stride);
          }
          return out + "]";
        }
        std::string object(const std::vector<harness_cpp_gpu__h_named>& values) {
          std::string out;
          for (const harness_cpp_gpu__h_named& nv : values) {
            out += ", " + q(nv.name) + ": " + nv.json;
          }
          return out;
        }
        void write(const std::string& path, const std::string& text) {
          std::ofstream f(path);
          f << text << "\\n";
        }
        }  // namespace

        void harness_cpp_gpu__parse_cases(const std::vector<std::string>& tokens, int ntokens,
                                          std::vector<std::string>& case_ids, int& ncases,
                                          bool& ok) {
          case_ids.clear();
          ok = ntokens >= 3 && tokens[0] == "--cases";
          for (int k = 2; ok && k < ntokens; ++k) {
            case_ids.push_back(tokens[static_cast<std::size_t>(k)]);
          }
          ncases = static_cast<int>(case_ids.size());
        }
        std::string harness_cpp_gpu__emit_real(dp x) { return real(x); }
        std::string harness_cpp_gpu__emit_int(int i) { return std::to_string(i); }
        std::string harness_cpp_gpu__emit_bool(bool b) { return b ? "true" : "false"; }
        EMITTERS
        harness_cpp_gpu__h_named harness_cpp_gpu__box(const std::string& name,
                                                      const std::string& json) {
          harness_cpp_gpu__h_named nv{};
          nv.name = name;
          nv.json = json;
          return nv;
        }
        void harness_cpp_gpu__write_snapshot(const std::string& case_id,
                                             const std::vector<harness_cpp_gpu__h_named>& values,
                                             dp time) {
          write("raw/state_snapshots/" + case_id + ".json",
                "{\\"t\\": " + real(time) + object(values) + "}");
        }
        void harness_cpp_gpu__write_metrics_basis(
            const std::vector<harness_cpp_gpu__h_mb_entry>& entries, int n) {
          std::string out = "{\\"per_test\\": [";
          for (int k = 0; k < n; ++k) {
            const harness_cpp_gpu__h_mb_entry& e = entries[static_cast<std::size_t>(k)];
            out += (k > 0 ? ", " : "") + std::string("{\\"test_id\\": ") + q(e.test_id)
                   + ", \\"case_id\\": " + q(e.case_id) + object(e.values) + "}";
          }
          write("raw/metrics_basis.json", out + "]}");
        }
        void harness_cpp_gpu__write_diagnostics(
            const std::vector<harness_cpp_gpu__h_case_result>& results, int n) {
          std::string out = "{\\"per_case\\": {";
          for (int k = 0; k < n; ++k) {
            const harness_cpp_gpu__h_case_result& r = results[static_cast<std::size_t>(k)];
            out += (k > 0 ? ", " : "") + q(r.case_id) + ": {\\"expected_xfail\\": "
                   + (r.expected_xfail ? "true" : "false") + ", \\"checks\\": {";
            for (std::size_t c = 0; c < r.checks.size(); ++c) {
              out += (c > 0 ? ", " : "") + q(r.checks[c].id) + ": " + q(trim(r.checks[c].status));
            }
            out += "}, \\"metrics\\": {";
            for (std::size_t m = 0; m < r.metrics.size(); ++m) {
              out += (m > 0 ? ", " : "") + q(r.metrics[m].name) + ": "
                     + (r.metrics[m].is_na ? q("na:" + r.metrics[m].reason_na)
                                           : real(r.metrics[m].value));
            }
            out += "}}";
          }
          write("diagnostics.json", out + "}}");
        }
        void harness_cpp_gpu__write_perf(const std::string& case_id, const std::string& target,
                                         int steps, int cells_updated, dp walltime_sec,
                                         int mpi_ranks, int threads_per_rank, int gpu_devices) {
          write("perf.json", "{\\"case_id\\": " + q(case_id) + ", \\"target\\": " + q(target)
                + ", \\"steps\\": " + std::to_string(steps) + ", \\"cells_updated\\": "
                + std::to_string(cells_updated) + ", \\"walltime_positive\\": "
                + (walltime_sec > 0.0 ? "true" : "false") + ", \\"mpi_ranks\\": "
                + std::to_string(mpi_ranks) + ", \\"threads_per_rank\\": "
                + std::to_string(threads_per_rank) + ", \\"gpu_devices\\": "
                + std::to_string(gpu_devices) + "}");
        }
        }  // namespace harness_cpp_gpu_model
        """).replace("EMITTERS\n", "".join(emitters))


def _checks_stub(sid: str, bound: list[tuple[str, int]], *, allocate: bool = True) -> str:
    """A checks source defining the ABI and `bound` state (name, rank): each array sized 2 per
    extent and filled with its element index; one check id `c1` passes on every case but an
    `_xfail` one; metrics `m.one` (a value) and `m.two` (not applicable)."""
    lines = [f'#include "{sid}_checks.cuh"', "", f"namespace {sid}_checks {{"]
    setup: list[str] = []
    for name, rank in bound:
        if rank == 0:
            lines.append(f"double {name} = 0.0;")
            setup.append(f"  {name} = 1.5;")
        elif rank == 1:
            lines.append(f"std::vector<double> {name};")
            setup.append(f"  {name}.assign(3, 2.0);")
        else:
            lines.append(f"atmofab::Array<double, {rank}> {name};")
            setup.append(f"  {name}.data.assign({2 ** rank}, 0.0);")
            setup.append(f"  for (int k = 0; k < {rank}; ++k) {{")
            setup.append(f"    {name}.extent[k] = 2;")
            setup.append("  }")
            setup.append(f"  for (int k = 0; k < {2 ** rank}; ++k) {{")
            setup.append(f"    {name}.data[static_cast<std::size_t>(k)] = k;")
            setup.append("  }")
    lines += [
        "",
        "void case_setup(const std::string& case_id, bool& ok) {",
        "  ok = case_id.find(\"xfail\") == std::string::npos;",
        *(setup if allocate else ["  (void)0;"]),
        "}",
        "",
        "void case_run(const std::string& case_id, int& steps, int& cells_updated, bool& ok) {",
        "  (void)case_id;",
        "  steps = 2;",
        "  cells_updated = 8;",
        "  ok = true;",
        "}",
        "",
        "void get_time(double& t) { t = 0.5; }",
        "",
        "void checks_compute(const std::string& case_id, const std::string& check_id,",
        "                    std::string& status) {",
        "  if (check_id == \"c1\") {",
        "    status = case_id.find(\"xfail\") == std::string::npos ? \"pass\" : \"fail\";",
        "  } else {",
        "    status = \"na  \";",
        "  }",
        "}",
        "",
        "void metric_compute(const std::string& case_id, const std::string& name, double& val,",
        "                    bool& is_na, std::string& reason_na, bool& found) {",
        "  (void)case_id;",
        "  found = name == \"m.one\" || name == \"m.two\";",
        "  is_na = name == \"m.two\";",
        "  val = 0.25;",
        "  reason_na = is_na ? \"not_computed\" : \"\";",
        "}",
        f"}}  // namespace {sid}_checks",
    ]
    return "\n".join(lines) + "\n"


def _smoke_ir() -> dict:
    """A problem IR with every rank (0..4), two cases (one xfail), a check id that needs C++
    escaping, two metrics and a multi-target test."""
    ir = copy.deepcopy(_rank34_metrics_ir())
    schema = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]
    schema["variables"] = [
        {"name": "s", "shape_expr": "scalar"}, {"name": "u", "shape_expr": "[3]"},
        {"name": "a2", "shape_expr": "[2, 2]"}, {"name": "a3", "shape_expr": "[2, 2, 2]"},
        {"name": "a4", "shape_expr": "[2, 2, 2, 2]"}]
    ir["case"]["test_case_set"] = [{"case_id": "c0"}, {"case_id": "c1_xfail"}]
    io = ir["io_contract"]
    io["test_evidence_requirements"] = [
        {"test_id": "c0", "required_raw_variables": ["a3", "a4", "s"]},
        {"test_id": "multi", "required_raw_variables": ["u"]},
        {"test_id": "guard", "required_raw_variables": ["s"]}]
    io["test_predicates"] = [
        {"test_id": "c0", "expected_outcome": "pass", "target_cases": ["c0"]},
        {"test_id": "multi", "expected_outcome": "pass", "target_cases": ["c1_xfail", "c0"]},
        {"test_id": "guard", "expected_outcome": "xfail", "target_cases": ["c1_xfail"]}]
    io["diagnostics_contract"]["checks"] = [{"id": "c1"}, {"id": 'q"?\\x'}]
    return ir


class RenderShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _render(_smoke_ir(), RANK_SID)

    def test_includes_the_two_headers_and_the_standard_library_only(self) -> None:
        includes = re.findall(r'^#include (.*)$', self.text, re.MULTILINE)
        self.assertEqual(['"harness_cpp_gpu_model.cuh"', f'"{RANK_SID}_checks.cuh"', "<chrono>",
                          "<cstddef>", "<cstdlib>", "<iostream>", "<string>", "<type_traits>",
                          "<vector>"], includes)
        self.assertIn("int main(int argc, char** argv) {", self.text)

    def test_the_harness_parameters_are_pinned_where_the_runner_compiles(self) -> None:
        self.assertIn("static_assert(std::is_same<hm::dp, double>::value,", self.text)
        self.assertIn(f"static_assert(hm::case_id_len == {cpp_runner.CASE_ID_LEN},", self.text)

    def test_each_rank_is_captured_by_its_emitter_behind_its_bound_check(self) -> None:
        self.assertIn('hm::harness_cpp_gpu__emit_real(ck::s)', self.text)
        self.assertIn('require_bound(!ck::u.empty(), "u", cid);', self.text)
        self.assertIn("hm::harness_cpp_gpu__emit_array_r1(view)", self.text)
        for rank in (2, 3, 4):
            self.assertIn(f"atmofab::View<const double, {rank}> view{{ck::a{rank}.data.data(),",
                          self.text)
            self.assertIn(f"hm::harness_cpp_gpu__emit_array_r{rank}(view)", self.text)
            self.assertIn(f"ck::a{rank}.data.size() == ", self.text)

    def test_capture_precedes_every_callback_of_the_case(self) -> None:
        order = [self.text.index(needle) for needle in (
            "ck::case_setup(cid, setup_ok);", "std::vector<Named> initial = capture_state(cid);",
            'harness_cpp_gpu__write_snapshot("initial/" + cid', "ck::case_run(cid,",
            "std::vector<Named> final_state = capture_state(cid);",
            "harness_cpp_gpu__write_snapshot(cid, final_state", "ck::checks_compute(",
            "ck::metric_compute(")]
        self.assertEqual(sorted(order), order)
        self.assertLess(self.text.index("std::vector<Named> initial = capture_state(cid);"),
                        self.text.index("ck::get_time(tval);"))

    def test_every_check_id_and_metric_is_driven_with_an_escaped_literal(self) -> None:
        self.assertIn('ck::checks_compute(cid, "c1", cstatus);', self.text)
        self.assertIn('ck::checks_compute(cid, "q\\"\\?\\\\x", cstatus);', self.text)
        self.assertIn('ck::metric_compute(cid, "m.one", mval, mis_na, mreason, mfound);',
                      self.text)
        self.assertIn('result.expected_xfail = cid == "c1_xfail";', self.text)

    def test_a_node_without_metrics_calls_no_metric_compute(self) -> None:
        text = _render(_boundary_ir(), BOUNDARY_SID)
        self.assertNotIn("metric_compute", text)
        self.assertNotIn("ck::metric", text)

    def test_the_writers_run_in_the_fortran_runner_s_order_with_the_target_s_parallelism(
            self) -> None:
        order = [self.text.index(needle) for needle in (
            "harness_cpp_gpu__write_metrics_basis(mb_entries, 4);",
            "harness_cpp_gpu__write_diagnostics(results, ncases);",
            "harness_cpp_gpu__write_perf(")]
        self.assertEqual(sorted(order), order)
        self.assertIn('"gpu", steps_total,\n      cells_total, walltime, 1, 3, 1);', self.text)
        cpu = _render(_smoke_ir(), RANK_SID, CPU_TARGET)
        self.assertIn('"cpu", steps_total,\n      cells_total, walltime, 1, 2, 0);', cpu)

    def test_no_serialization_is_spelled_by_the_runner(self) -> None:
        for forbidden in ("printf", "hexfloat", "ofstream", "fopen", "\\\"{", ".json\""):
            self.assertNotIn(forbidden, self.text)

    def test_the_rendered_runner_passes_the_runner_scans(self) -> None:
        out: list[str] = []
        cpp_source.validate_runner_json_serialization(Path("r.cu"), self.text.lower(), out)
        cpp_source.validate_runner_snapshot_filenames(Path("r.cu"), self.text.lower(), out,
                                                      known_case_ids={"c0", "c1_xfail"})
        self.assertEqual([], out)


class MultiTargetMetricsBasisTest(unittest.TestCase):
    def test_one_entry_per_test_and_target_case(self) -> None:
        text = _render(_smoke_ir(), RANK_SID)
        entries = re.findall(r'entry\.test_id = "([^"]+)";\n    entry\.case_id = "([^"]+)";',
                             text)
        self.assertEqual([("c0", "c0"), ("multi", "c1_xfail"), ("multi", "c0"),
                          ("guard", "c1_xfail")], entries)


class DeterminismTest(unittest.TestCase):
    def test_render_is_deterministic(self) -> None:
        self.assertEqual(_render(_smoke_ir(), RANK_SID), _render(_smoke_ir(), RANK_SID))
        self.assertEqual(cpp_runner.render_checks_header(_smoke_ir(), RANK_SID),
                         cpp_runner.render_checks_header(_smoke_ir(), RANK_SID))


class ChecksHeaderTest(unittest.TestCase):
    def test_the_header_declares_the_abi_and_the_bound_state_by_rank(self) -> None:
        name, text = cpp_runner.render_checks_header(_smoke_ir(), RANK_SID)
        self.assertEqual(f"{RANK_SID}_checks.cuh", name)
        self.assertIn("#pragma once", text)
        self.assertIn(cpp_header.VIEW_DEFINITION, text)
        self.assertIn(f"namespace {RANK_SID}_checks {{", text)
        for callback in (
                "void case_setup(const std::string& case_id, bool& ok);",
                ("void case_run(const std::string& case_id, int& steps, int& cells_updated, "
                 "bool& ok);"),
                "void get_time(double& t);",
                ("void checks_compute(const std::string& case_id, const std::string& check_id, "
                 "std::string& status);"),
                ("void metric_compute(const std::string& case_id, const std::string& name, "
                 "double& val, bool& is_na, std::string& reason_na, bool& found);")):
            self.assertIn(callback, text)
        for decl in ("extern double s;", "extern std::vector<double> u;",
                     "extern atmofab::Array<double, 2> a2;", "extern atmofab::Array<double, 3> a3;",
                     "extern atmofab::Array<double, 4> a4;"):
            self.assertIn(decl, text)

    def test_the_declared_parameter_types_are_what_the_checks_gate_compares(self) -> None:
        """The header is rendered from `CHECKS_ABI_PARAMS`, and the gate compares a definition's
        types against the same table — so a definition copied from the header is accepted."""
        _name, text = cpp_runner.render_checks_header(_smoke_ir(), RANK_SID)
        decls = [fn for fn in cpp_source.cpp_decls.read(text).functions]
        self.assertEqual(list(cpp_checks_abi.CHECKS_PUBLIC_NAMES), [fn.name for fn in decls])
        for fn in decls:
            self.assertEqual(tuple(t for t, _n in cpp_checks_abi.CHECKS_ABI_PARAMS[fn.name]),
                             tuple(t for t, _n in fn.params))

    def test_fortran_renders_none(self) -> None:
        self.assertIsNone(ft_runner.render_checks_header(_boundary_ir(), BOUNDARY_SID))


#: The Fortran fail-close rows whose refusal is FORTRAN's: the 100-column lint limit its rendered
#: lines are held to, and the spec_id bound its 63-character identifier limit implies. Every
#: other row is a refusal of a neutral IR fact and must refuse in both renderers.
_FORTRAN_ONLY_REFUSALS = frozenset({"over_100_col_line", "over_long_spec_id"})


class RenderErrorDifferentialTest(unittest.TestCase):
    """Every compile gate runs every declared renderer's `ir_content_violations` (issue #284), so
    a refusal one renderer makes of a neutral fact and the other does not fails a Fortran node's
    Compile for a CUDA reason or the reverse. The two share `tools/runner_ir.py`; this pins the
    outcome over the Fortran renderer's own fail-close table."""

    def test_the_fortran_fail_close_table_refuses_here_except_fortran_s_own_rows(self) -> None:
        labels = {label for label, *_rest in ft_tests._RENDER_FAILCLOSE_CASES}
        self.assertLessEqual(_FORTRAN_ONLY_REFUSALS, labels)
        for label, mutate, spec_id, is_identity in ft_tests._RENDER_FAILCLOSE_CASES:
            with self.subTest(label):
                ir = copy.deepcopy(_boundary_ir())
                if mutate is not None:
                    mutate(ir)
                if label in _FORTRAN_ONLY_REFUSALS:
                    _render(ir, spec_id)  # renders: no C++ reason to refuse it
                    self.assertEqual([], cpp_runner.ir_content_violations(ir, spec_id, HARNESS))
                    continue
                with self.assertRaises(RenderError):
                    _render(ir, spec_id)
                violations = cpp_runner.ir_content_violations(ir, spec_id, HARNESS)
                self.assertEqual(is_identity, violations == [], violations)

    def test_the_clean_fixtures_render_in_both(self) -> None:
        for ir, sid in ((_boundary_ir(), BOUNDARY_SID), (_metrics_ir(), "prob_x"),
                        (_rank34_metrics_ir(), RANK_SID), (_smoke_ir(), RANK_SID)):
            with self.subTest(sid):
                self.assertEqual([], cpp_runner.ir_content_violations(ir, sid, HARNESS))
                self.assertEqual([], ft_runner.ir_content_violations(
                    ir, sid, "harness_fortran_cpu"))

    def test_the_cuda_only_refusals_are_names_c_plus_plus_cannot_declare(self) -> None:
        for name in ("int", "namespace", "case_run", "std", "atmofab", "__x"):
            with self.subTest(name):
                ir = copy.deepcopy(_boundary_ir())
                ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
                    "variables"][3]["name"] = name
                ir["io_contract"]["test_evidence_requirements"][2][
                    "required_raw_variables"] = [name]
                violations = cpp_runner.ir_content_violations(ir, BOUNDARY_SID, HARNESS)
                self.assertTrue(violations and name in violations[0], violations)

    def test_a_non_iterable_field_is_a_violation_not_an_exception(self) -> None:
        ir = copy.deepcopy(_boundary_ir())
        ir["io_contract"]["diagnostics_contract"]["checks"] = 5
        self.assertTrue(cpp_runner.ir_content_violations(ir, BOUNDARY_SID, HARNESS))

    def test_a_control_character_has_no_literal(self) -> None:
        with self.assertRaises(RenderError):
            cpp_runner._clit("a\nb")
        self.assertEqual('a\\"b\\?\\?=\\\\', cpp_runner._clit('a"b??=\\'))


class HarnessPinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ir = _smoke_ir()
        self.source = _harness_stub()

    def _pin(self, signatures=None, source=None, harness=HARNESS) -> None:
        cpp_runner.assert_harness_pin(
            self.ir, RANK_SID, harness,
            _harness_signatures() if signatures is None else signatures,
            self.source if source is None else source)

    def test_the_pinned_interface_is_the_controlled_spec_s(self) -> None:
        spec = Path(__file__).resolve().parents[2] / (
            "spec/infrastructure/infra/harness/harness_cpp_gpu/controlled_spec.md")
        block = re.search(r"### 5\.1.*?```yaml\n(.*?)```", spec.read_text(encoding="utf-8"),
                          re.DOTALL)
        self.assertIsNotNone(block)
        self.assertEqual(block.group(1), cpp_runner._HARNESS_V1_INTERFACE)

    def test_a_clean_harness_passes(self) -> None:
        self._pin()

    def test_another_harness_is_refused(self) -> None:
        with self.assertRaisesRegex(RenderError, "not the pinned"):
            self._pin(harness="harness_other")

    def test_no_usable_signatures_is_a_missing_artifact(self) -> None:
        for sigs in ([], [{"symbol": "", "signature": {}}], "nope"):
            with self.subTest(repr(sigs)), self.assertRaisesRegex(
                    RenderError, "no usable public_api.signatures"):
                self._pin(signatures=sigs)

    def test_ir_drift_in_a_procedure_is_refused(self) -> None:
        sigs = _harness_signatures()
        write_perf = next(s for s in sigs if s["symbol"] == "harness_cpp_gpu__write_perf")
        write_perf["signature"]["args"][6]["spec"] = {"type": "real", "kind": "dp"}
        with self.assertRaisesRegex(RenderError, "IR signature for 'harness_cpp_gpu__write_perf'"):
            self._pin(signatures=sigs)

    def test_ir_drift_in_a_type_s_member_order_is_refused(self) -> None:
        sigs = _harness_signatures()
        metric = next(s for s in sigs if s["symbol"] == "harness_cpp_gpu__h_metric")
        metric["signature"]["components"].reverse()
        with self.assertRaisesRegex(RenderError, "IR signature for 'harness_cpp_gpu__h_metric'"):
            self._pin(signatures=sigs)

    def test_an_omitted_symbol_is_refused(self) -> None:
        sigs = [s for s in _harness_signatures() if s["symbol"] != "harness_cpp_gpu__box"]
        with self.assertRaisesRegex(RenderError, "omits 'harness_cpp_gpu__box'"):
            self._pin(signatures=sigs)

    def test_source_drift_is_refused(self) -> None:
        drifted = self.source.replace(
            "void harness_cpp_gpu__write_snapshot(const std::string& case_id,",
            "void harness_cpp_gpu__write_snapshot(const std::string& name,")
        self.assertNotEqual(drifted, self.source)
        with self.assertRaisesRegex(RenderError, "model source signature for "
                                                 "'harness_cpp_gpu__write_snapshot'"):
            self._pin(source=drifted)
        with self.assertRaisesRegex(RenderError, "model source omits"):
            self._pin(source="namespace harness_cpp_gpu_model {}\n")

    def test_only_the_emitters_the_node_uses_are_pinned(self) -> None:
        sigs = [s for s in _harness_signatures()
                if s["symbol"] != "harness_cpp_gpu__emit_array_r4"]
        self.ir = _boundary_ir()  # ranks 0 and 2 only
        self._pin(signatures=sigs)


def _nvcc_lint(directory: Path, sources: list[str]) -> subprocess.CompletedProcess:
    from tools.backends.linter.nvcc import lint as nvcc_lint
    return subprocess.run(list(nvcc_lint.source_argv(sources)), cwd=directory,
                          capture_output=True, text=True, check=False)


@unittest.skipUnless(NVCC, "the CUDA compiler driver is not installed")
class NvccSmokeTest(unittest.TestCase):
    """The rendered runner and checks header, the host-rendered harness header, a host-only
    harness stub and a checks stub: held to the declared lint rule set, then built and RUN (no
    kernel, so no device is needed)."""

    def _tree(self, d: Path, ir: dict, sid: str, *, allocate: bool = True) -> None:
        (d / "harness_cpp_gpu_model.cuh").write_text(
            cpp_header.render(HARNESS, _harness_public_api()))
        (d / "harness_cpp_gpu_model.cu").write_text(_harness_stub())
        (d / f"{sid}_runner.cu").write_text(_render(ir, sid))
        name, text = cpp_runner.render_checks_header(ir, sid)
        (d / name).write_text(text)
        bound = cpp_runner._bound_state(ir)
        (d / f"{sid}_checks.cu").write_text(_checks_stub(sid, bound, allocate=allocate))

    def _build(self, d: Path, sid: str) -> None:
        r = subprocess.run([NVCC, "-std=c++17", "-O2", "-I.", f"{sid}_runner.cu",
                            f"{sid}_checks.cu", "harness_cpp_gpu_model.cu", "-o", "runner"],
                           cwd=d, capture_output=True, text=True, check=False)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        (d / "raw" / "state_snapshots" / "initial").mkdir(parents=True)

    def test_the_rendered_files_pass_the_declared_lint_rules(self) -> None:
        for ir, sid in ((_smoke_ir(), RANK_SID), (_boundary_ir(), BOUNDARY_SID)):
            with self.subTest(sid), tempfile.TemporaryDirectory() as td:
                d = Path(td)
                self._tree(d, ir, sid)
                r = _nvcc_lint(d, [f"{sid}_runner.cu", f"{sid}_checks.cu",
                                   "harness_cpp_gpu_model.cu"])
                self.assertEqual(0, r.returncode, r.stdout + r.stderr)

    def test_a_definition_of_another_type_is_a_compile_error(self) -> None:
        """The reason the host renders the checks header: without it a `float` storage of a
        `double` bound variable links silently."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._tree(d, _smoke_ir(), RANK_SID)
            checks = d / f"{RANK_SID}_checks.cu"
            checks.write_text(checks.read_text().replace("double s = 0.0;", "float s = 0.0;"))
            r = _nvcc_lint(d, [checks.name])
            self.assertNotEqual(0, r.returncode)
            self.assertIn("declaration is incompatible with", r.stdout + r.stderr)
            self.assertIn(f"{RANK_SID}_checks.cuh", r.stdout + r.stderr)

    def test_the_runner_writes_every_output_through_the_harness(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._tree(d, _smoke_ir(), RANK_SID)
            self._build(d, RANK_SID)
            r = subprocess.run(["./runner", "--cases", "spec.yaml", "c0", "c1_xfail"], cwd=d,
                               capture_output=True, text=True, check=False)
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            final = json.loads((d / "raw/state_snapshots/c0.json").read_text())
            initial = json.loads((d / "raw/state_snapshots/initial/c0.json").read_text())
            self.assertEqual({"t", "s", "u", "a2", "a3", "a4"}, set(final))
            self.assertEqual(0.5, final["t"])
            self.assertEqual([[0.0, 1.0], [2.0, 3.0]], final["a2"])
            self.assertEqual(1.5, initial["s"])
            diagnostics = json.loads((d / "diagnostics.json").read_text())["per_case"]
            self.assertEqual({"c1": "pass", 'q"?\\x': "na"}, diagnostics["c0"]["checks"])
            self.assertTrue(diagnostics["c1_xfail"]["expected_xfail"])
            self.assertEqual({"m.one": 0.25, "m.two": "na:not_computed"},
                             diagnostics["c0"]["metrics"])
            basis = json.loads((d / "raw/metrics_basis.json").read_text())["per_test"]
            self.assertEqual([("c0", "c0"), ("multi", "c1_xfail"), ("multi", "c0"),
                              ("guard", "c1_xfail")],
                             [(e["test_id"], e["case_id"]) for e in basis])
            self.assertFalse(diagnostics["c0"]["expected_xfail"])
            perf = json.loads((d / "perf.json").read_text())
            self.assertEqual({"case_id": "c1_xfail", "target": "gpu", "steps": 4,
                              "cells_updated": 16, "walltime_positive": True, "mpi_ranks": 1,
                              "threads_per_rank": 3, "gpu_devices": 1}, perf)

    def test_an_unbound_array_stops_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._tree(d, _smoke_ir(), RANK_SID, allocate=False)
            self._build(d, RANK_SID)
            r = subprocess.run(["./runner", "--cases", "spec.yaml", "c0"], cwd=d,
                               capture_output=True, text=True, check=False)
            self.assertEqual(1, r.returncode)
            self.assertIn("bound state u is not allocated at capture for case c0", r.stderr)

    def test_a_missing_case_list_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._tree(d, _smoke_ir(), RANK_SID)
            self._build(d, RANK_SID)
            r = subprocess.run(["./runner"], cwd=d, capture_output=True, text=True, check=False)
            self.assertEqual(1, r.returncode)
            self.assertIn("--cases <spec> <case_id>... required", r.stderr)


class SignaturesRoundTripTest(unittest.TestCase):
    def test_the_harness_stub_satisfies_the_section_5_1_source_pin(self) -> None:
        """The stub is a faithful harness: its definitions carry exactly the pinned atoms."""
        ops, _types, _ifaces, errors = cpp_signatures._stanzas(
            cpp_source.cpp_decls.read(cpp_header.render(HARNESS, _harness_public_api())
                                      + _harness_stub()), (f"{HARNESS}_model",))
        self.assertEqual([], errors)
        self.assertLessEqual({s["symbol"] for s in _harness_signatures()
                              if "args" in s["signature"]}, set(ops))


if __name__ == "__main__":
    unittest.main()
