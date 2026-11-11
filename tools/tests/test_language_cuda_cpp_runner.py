"""The CUDA C++ backend's `runner_render` capability (issue #289, R4-b PR-6; the driver, issue #589).

`render_runner` is a pure function of the IR: these tests pin the rendered shape, determinism, the
render-error matrix AGAINST the Fortran renderer's (the two share the neutral IR readers,
`tools/runner_ir.py`, so an IR one target's renderer accepts is not refused by the other's for a
neutral reason), and the harness signature pin. The rows that run the CUDA compiler driver — the
declared lint rule set over the rendered files, and a smoke that compiles, links and RUNS the
rendered runner against the reading harness of `data/driver_e2e/` and a host-only model (no kernel,
so no device is needed) — are skipped where `nvcc` is not on `PATH`. What the driver promises per
case is `test_driver_render`'s.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from tools import primary_evidence as pe
from tools.backends.language.cuda_cpp import header as cpp_header
from tools.backends.language.cuda_cpp import runner as cpp_runner
from tools.backends.language.cuda_cpp import signatures as cpp_signatures
from tools.backends.language.cuda_cpp import source as cpp_source
from tools.backends.language.fortran import runner as ft_runner
from tools.host_render import RenderError
from tools.structured_signatures import load_structured_signatures
from tools.tests import skip_probes
from tools.tests import test_fortran_runner as ft_tests
from tools.tests.test_driver_render import call_ir, init_ir, loop_ir, procedure_ir

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


DRIVER_E2E = Path(__file__).parent / "data" / "driver_e2e"


def _harness_stub() -> str:
    """A host-only harness: every §5.1 operation defined in namespace `harness_cpp_gpu_model`, its
    declarations from the host-rendered header, as a certified harness's are."""
    return (DRIVER_E2E / "harness_cpp_gpu_model.cu").read_text()


SMOKE_OP = f"{RANK_SID}__apply"


def _smoke_ir() -> dict:
    """A problem IR with every rank (0..4), two cases (one refused) and a multi-target test."""
    ir = copy.deepcopy(_rank34_metrics_ir())
    schema = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]
    schema["variables"] = [
        {"name": "s", "shape_expr": "scalar"}, {"name": "u", "shape_expr": "[3]"},
        {"name": "a2", "shape_expr": "[2, 2]"}, {"name": "a3", "shape_expr": "[2, 2, 2]"},
        {"name": "a4", "shape_expr": "[2, 2, 2, 2]"}]
    a2 = [[0.0, 1.0], [2.0, 3.0]]
    a3 = [a2, a2]
    initial = {"s": 1.5, "u": [2.0, 2.0, 2.0], "a2": a2, "a3": a3, "a4": [a3, a3]}
    ir["case"]["test_case_set"] = [
        {"case_id": "c0", "inputs": {"mode": 0}, "initial_state": initial},
        {"case_id": "c1_xfail", "inputs": {"mode": 1}, "initial_state": initial}]
    io = ir["io_contract"]
    io["tests"] = [
        {"test_id": "c0", "expected_outcome": "pass", "target_cases": ["c0"], "quantities": ["q"]},
        {"test_id": "multi", "expected_outcome": "pass", "target_cases": ["c1_xfail", "c0"],
         "quantities": ["q"]},
        {"test_id": "guard", "expected_outcome": "xfail", "target_cases": ["c1_xfail"],
         "quantities": ["q"]}]
    return ft_tests._driven(
        {**ir, "io_contract": {**io, "raw_requirements": {"required_evidence": [
            {"artifact": "state_snapshots", "schema": schema}]}}},
        SMOKE_OP, {"s": ("inout", 0), "u": ("inout", 1), "a2": ("inout", 2), "a3": ("inout", 3),
                   "a4": ("inout", 4)})


def _smoke_model() -> str:
    """The operation `_smoke_ir` drives: `s` doubled, every array's first cell set; mode 1 refuses
    with nothing written. Host code."""
    return textwrap.dedent(f"""\
        #include "{RANK_SID}_model.cuh"

        namespace {RANK_SID}_model {{
        void {SMOKE_OP}(int mode, dp& s, spec2science::View<dp, 1> u, spec2science::View<dp, 2> a2,
                        spec2science::View<dp, 3> a3, spec2science::View<dp, 4> a4, bool& ok) {{
          ok = mode == 0;
          if (!ok) {{
            return;
          }}
          s = 2.0 * s + spec2science::launch_block_size;
          u.data[0] = 9.0;
          a2.data[0] = 9.0;
          a3.data[0] = 9.0;
          a4.data[0] = 9.0;
        }}
        }}  // namespace {RANK_SID}_model
        """)


class RenderShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _render(_smoke_ir(), RANK_SID)

    def test_includes_the_two_headers_and_the_standard_library_only(self) -> None:
        includes = re.findall(r'^#include (.*)$', self.text, re.MULTILINE)
        self.assertEqual(['"harness_cpp_gpu_model.cuh"', f'"{RANK_SID}_model.cuh"', "<algorithm>",
                          "<chrono>", "<cmath>", "<cstddef>", "<cstdint>", "<cstdio>", "<cstdlib>",
                          "<iostream>", "<limits>", "<string>", "<type_traits>", "<vector>"],
                         includes)
        self.assertIn("int main(int argc, char** argv) {", self.text)

    def test_the_harness_parameters_are_pinned_where_the_runner_compiles(self) -> None:
        self.assertIn("static_assert(std::is_same<hm::dp, double>::value,", self.text)
        self.assertIn(f"static_assert(hm::case_id_len == {cpp_runner.CASE_ID_LEN},", self.text)

    def test_each_rank_is_captured_by_its_emitter_from_the_driver_s_own_state(self) -> None:
        self.assertIn("hm::harness_cpp_gpu__emit_real(st_s)", self.text)
        self.assertIn("hm::harness_cpp_gpu__emit_array_r1(cv(st_u))", self.text)
        for rank in (2, 3, 4):
            self.assertIn(f"hm::harness_cpp_gpu__emit_array_r{rank}(cv(st_a{rank}))", self.text)
            self.assertIn(f"spec2science::Array<double, {rank}> st_a{rank}{{}};", self.text)
        self.assertIn("std::vector<double> st_u{};", self.text)

    def test_the_captures_bracket_the_call_and_the_case_file_is_read_before_either(self) -> None:
        """The case file read whole, the initial capture on what it held, the operation, the
        final capture on what it left, and the perf record after every case."""
        order = [self.text.index(needle) for needle in (
            "hm::harness_cpp_gpu__read_case(cid, ok);",
            'hm::harness_cpp_gpu__get_array_r2("a2", st_a2);',
            'write_capture("initial/" + cid);',
            f"md::{SMOKE_OP}(",
            "write_capture(cid);",
            "hm::harness_cpp_gpu__write_perf(")]
        self.assertEqual(sorted(order), order)
        self.assertEqual(1, self.text.count(f"md::{SMOKE_OP}("))

    def test_the_imposed_block_size_is_set_before_any_operation(self) -> None:
        """Issue #512 (PR-4c): the harness's `__block_size` is called once, after the case set
        is parsed and before the first operation, and its value is stored where the node's
        sources read it (`spec2science::launch_block_size`, declared by the rendered header)."""
        call = "  spec2science::launch_block_size = hm::harness_cpp_gpu__block_size();\n"
        self.assertEqual(1, self.text.count(call))
        self.assertEqual(1, self.text.count("harness_cpp_gpu__block_size("))
        self.assertLess(self.text.index("harness_cpp_gpu__parse_cases("), self.text.index(call))
        self.assertLess(self.text.index(call), self.text.index(f"md::{SMOKE_OP}("))
        header = cpp_header.render(HARNESS, _harness_public_api(), spec_kind="infrastructure")
        self.assertIn("inline int launch_block_size = 0;", header)

    def test_the_writers_run_in_the_fortran_runner_s_order_with_the_target_s_parallelism(
            self) -> None:
        self.assertLess(self.text.rindex("harness_cpp_gpu__write_snapshot("),
                        self.text.index("harness_cpp_gpu__write_perf("))
        cells = ("steps_total,\n      static_cast<int>(std::min<std::int64_t>(cells_total, "
                 "std::numeric_limits<int>::max())),\n      walltime, ")
        self.assertIn(f'"gpu", {cells}1, 3, 1);', self.text)
        cpu = _render(_smoke_ir(), RANK_SID, CPU_TARGET)
        self.assertIn(f'"cpu", {cells}1, 2, 0);', cpu)

    def test_no_serialization_is_spelled_by_the_runner(self) -> None:
        for forbidden in ("printf", "hexfloat", "ofstream", "fopen", "\\\"{", ".json\""):
            self.assertNotIn(forbidden, self.text)

    def test_the_rendered_runner_passes_the_runner_scans(self) -> None:
        out: list[str] = []
        cpp_source.validate_runner_json_serialization(Path("r.cu"), self.text.lower(), out)
        cpp_source.validate_runner_snapshot_filenames(Path("r.cu"), self.text.lower(), out,
                                                      known_case_ids={"c0", "c1_xfail"})
        self.assertEqual([], out)


class RenderedRunnerThroughTheModelGatesTest(unittest.TestCase):
    """Round 4 of this change's review: `model_source_gates` holds every `.cu` of `src/` to the
    leaf's preprocessor allowlist, and the host-rendered runner — which ends with `std::_Exit`, a
    reserved name — was refused on every physics node, unrepairably. The runner is not read on a
    physics node; on an `infrastructure` node the leaf authors it and it still is."""

    def _gates(self, node_key: str) -> list[str]:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / f"{RANK_SID}_runner.cu").write_text(_render(_smoke_ir(), RANK_SID))
            model = d / f"{RANK_SID}_model.cu"
            model.write_text(f"namespace {RANK_SID}_model {{}}\n")
            out: list[str] = []
            cpp_source.model_source_gates(node_key=node_key, model_file=model,
                                          text=model.read_text(), dep_spec_ids=[],
                                          violations=out, multidim_spec_id=None)
            return out

    def test_the_rendered_runner_passes_a_physics_node_s_model_gates(self) -> None:
        rendered = _render(_smoke_ir(), RANK_SID)
        self.assertIn("std::_Exit", rendered)
        # The device reset precedes `_Exit`: a kernel trace loses its records otherwise (#307).
        finish = rendered[rendered.index("void finish(int code)"):]
        self.assertLess(finish.index("cudaDeviceReset()"), finish.index("std::_Exit"))
        for kind in ("problem", "component"):
            with self.subTest(kind):
                self.assertEqual([], self._gates(f"{kind}/{RANK_SID}@0.1.0"))

    def test_a_leaf_authored_runner_is_still_read(self) -> None:
        out = self._gates(f"infrastructure/{RANK_SID}@0.1.0")
        self.assertTrue(any("_runner.cu" in v and "`_Exit`" in v for v in out), out)




class DeterminismTest(unittest.TestCase):
    def test_render_is_deterministic(self) -> None:
        self.assertEqual(_render(_smoke_ir(), RANK_SID), _render(_smoke_ir(), RANK_SID))


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
        for name in ("int", "namespace", "class"):
            with self.subTest(name):
                ir = copy.deepcopy(_boundary_ir())
                ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
                    "variables"][3]["name"] = name
                violations = cpp_runner.ir_content_violations(ir, BOUNDARY_SID, HARNESS)
                self.assertTrue(violations and name in violations[0], violations)

    def test_a_state_named_like_a_library_or_a_retired_callback_renders_under_its_prefix(
            self) -> None:
        # A state's variable is the driver's, `st_<name>`, in the driver's own scope.
        for name in ("case_run", "get_time", "std", "spec2science", "md", "hm"):
            with self.subTest(name):
                ir = copy.deepcopy(_boundary_ir())
                ft_tests._rename_state(ir, "guard_fired", name)
                self.assertEqual([], cpp_runner.ir_content_violations(ir, BOUNDARY_SID, HARNESS))
                self.assertIn(f"st_{name}", _render(ir, BOUNDARY_SID))

    def test_a_non_iterable_field_is_a_violation_not_an_exception(self) -> None:
        ir = copy.deepcopy(_boundary_ir())
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]["variables"] = 5
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
        named = next(s for s in sigs if s["symbol"] == "harness_cpp_gpu__h_named")
        named["signature"]["components"].reverse()
        with self.assertRaisesRegex(RenderError, "IR signature for 'harness_cpp_gpu__h_named'"):
            self._pin(signatures=sigs)

    def test_an_omitted_symbol_is_refused(self) -> None:
        sigs = [s for s in _harness_signatures() if s["symbol"] != "harness_cpp_gpu__box"]
        with self.assertRaisesRegex(RenderError, "omits 'harness_cpp_gpu__box'"):
            self._pin(signatures=sigs)

    def test_a_harness_without_the_block_size_is_refused(self) -> None:
        """Issue #512 (PR-4c): every runner calls `__block_size`, so a certified harness that
        does not publish it — `harness_cpp_gpu@0.1.0` — is refused before render, in its IR and
        in its model source alike, whatever ranks the node binds."""
        sigs = [s for s in _harness_signatures() if s["symbol"] != "harness_cpp_gpu__block_size"]
        self.assertLess(len(sigs), len(_harness_signatures()))
        for ir in (_smoke_ir(), _boundary_ir()):
            self.ir = ir
            with self.subTest(sid=sorted(ir)), self.assertRaisesRegex(
                    RenderError, "omits 'harness_cpp_gpu__block_size'"):
                self._pin(signatures=sigs)
        head, tail = self.source.split("int harness_cpp_gpu__block_size() {")
        cut = head + tail.split("\n}\n", 1)[1]
        self.assertNotIn("int harness_cpp_gpu__block_size()", cut)
        with self.assertRaisesRegex(RenderError,
                                    "model source omits 'harness_cpp_gpu__block_size'"):
            self._pin(source=cut)

    def test_source_drift_is_refused(self) -> None:
        drifted = self.source.replace(
            "void harness_cpp_gpu__write_snapshot(\n    const std::string& case_id,",
            "void harness_cpp_gpu__write_snapshot(\n    const std::string& name,")
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

    # Amendment 4 of issue #589: the pin is enforced for what `_used_harness_ops` lists.

    @staticmethod
    def _without(source: str, symbol: str) -> str:
        """`source` with the definition of the harness function `symbol` removed."""
        out, n = re.subn(rf"^[^\n]*\b{symbol}\(.*?^}}\n", "", source, count=1,
                         flags=re.S | re.M)
        if n != 1:
            raise AssertionError(f"{symbol} is not defined in the stub")
        return out

    def test_a_certified_source_missing_read_case_is_refused(self) -> None:
        symbol = "harness_cpp_gpu__read_case"
        self.assertIn("read_case", cpp_runner._used_harness_ops(self.ir))
        with self.assertRaisesRegex(RenderError, f"model source omits '{symbol}'"):
            self._pin(source=self._without(self.source, symbol))
        with self.assertRaisesRegex(RenderError, f"omits '{symbol}'"):
            self._pin(signatures=[s for s in _harness_signatures() if s["symbol"] != symbol])

    def test_the_getters_are_pinned_exactly_when_the_ir_uses_them(self) -> None:
        self.ir = _boundary_ir()
        used = cpp_runner._used_harness_ops(self.ir)
        for getter in ("get_array_r2", "get_real", "get_int"):
            self.assertIn(getter, used)
            symbol = f"harness_cpp_gpu__{getter}"
            with self.subTest(getter), self.assertRaisesRegex(RenderError, f"omits '{symbol}'"):
                self._pin(signatures=[s for s in _harness_signatures() if s["symbol"] != symbol])
        for getter in ("get_array_r1", "get_array_r3", "get_array_r4", "get_bool"):
            self.assertNotIn(getter, used)
            symbol = f"harness_cpp_gpu__{getter}"
            with self.subTest(getter):
                self._pin(signatures=[s for s in _harness_signatures() if s["symbol"] != symbol])

    def test_the_rank_1_emitter_is_pinned_on_a_loop_for_its_series(self) -> None:
        symbol = "harness_cpp_gpu__emit_array_r1"
        cut = [s for s in _harness_signatures() if s["symbol"] != symbol]
        self.ir = _boundary_ir()  # no rank-1 state
        self.assertNotIn("emit_array_r1", cpp_runner._used_harness_ops(self.ir))
        self._pin(signatures=cut)
        self.ir["drive"]["loop"] = {"n_step": "inputs.n_step", "dt": "inputs.dt",
                                    "t_start": "inputs.t_start", "t_end": "inputs.t_end"}
        self.ir["drive"]["history"] = {"every": "inputs.every"}
        self.assertIn("emit_array_r1", cpp_runner._used_harness_ops(self.ir))
        self._pin()
        with self.assertRaisesRegex(RenderError, f"omits '{symbol}'"):
            self._pin(signatures=cut)


def _nvcc_lint(directory: Path, sources: list[str]) -> subprocess.CompletedProcess:
    from tools.backends.linter.nvcc import lint as nvcc_lint
    return subprocess.run(list(nvcc_lint.source_argv(sources)), cwd=directory,
                          capture_output=True, text=True, check=False)


@skip_probes.skip_unless_installed("cuda_compiler_driver")
class NvccSmokeTest(unittest.TestCase):
    """The rendered runner, the host-rendered headers, a host-only harness and model: held to the
    declared lint rule set, then built and RUN (no kernel, so no device is needed)."""

    def _tree(self, d: Path, ir: dict, sid: str, model: str | None = None) -> None:
        (d / "ir").mkdir()
        (d / "ir" / "spec.ir.yaml").write_text(yaml.safe_dump(ir))
        pe.write_case_inputs(ir, d / "ir")
        (d / "harness_cpp_gpu_model.cuh").write_text(
            cpp_header.render(HARNESS, _harness_public_api(), spec_kind="infrastructure"))
        (d / "harness_cpp_gpu_model.cu").write_text(_harness_stub())
        (d / f"{sid}_runner.cu").write_text(_render(ir, sid))
        (d / f"{sid}_model.cuh").write_text(
            cpp_header.render(sid, ir["public_api"], spec_kind=ir["meta"]["spec_kind"]))
        (d / f"{sid}_model.cu").write_text(model if model is not None else _smoke_model())

    def _build(self, d: Path, sid: str) -> None:
        r = subprocess.run([NVCC, "-std=c++17", "-O2", "-I.", f"{sid}_runner.cu",
                            f"{sid}_model.cu", "harness_cpp_gpu_model.cu", "-o", "runner"],
                           cwd=d, capture_output=True, text=True, check=False)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        for sub in ("initial", "history", "series"):
            (d / "raw" / "state_snapshots" / sub).mkdir(parents=True)

    @staticmethod
    def _run(d: Path, *cases: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["./runner", "--cases", "ir/spec.ir.yaml", *cases], cwd=d,
                              capture_output=True, text=True, check=False,
                              env={**os.environ, "SPEC2SCIENCE_CUDA_BLOCK": "", **(env or {})})

    def _assert_lints_clean(self, ir: dict, sid: str, model: str) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._tree(d, ir, sid, model)
            r = _nvcc_lint(d, [f"{sid}_runner.cu"])
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)

    def test_the_rendered_files_pass_the_declared_lint_rules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._tree(d, _smoke_ir(), RANK_SID)
            r = _nvcc_lint(d, [f"{RANK_SID}_runner.cu", f"{RANK_SID}_model.cu",
                               "harness_cpp_gpu_model.cu"])
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)

    def test_each_shape_of_driver_passes_the_declared_lint_rules(self) -> None:
        """Not a repeat of `test_workflow_conductor.CudaCppLintAttributionTest
        .test_the_driver_of_every_shape_is_clean_under_the_declared_lint_set`, though both lint
        the four shapes' runners with the same argv: that row renders for
        `test_driver_render.GPU` (`threads_per_rank: 1`), this one for `GPU_TARGET` (3). Measured
        2026-11-11, on a branch that had dropped this row as a repeat: a runner lint finding
        rendered only for a loop node under `threads_per_rank != 1` turned this row red and
        every other lint row green (a finding under `threads_per_rank != 1` on any node is also
        caught by `test_the_rendered_files_pass_the_declared_lint_rules`)."""
        toy = (DRIVER_E2E / "drv_toy_model.cu").read_text()
        for label, ir in (("loop", loop_ir(0)), ("init", init_ir(0)), ("call", call_ir(0)),
                          ("procedure", procedure_ir())):
            with self.subTest(label):
                self._assert_lints_clean(ir, "drv_toy", toy)

    def test_the_runner_writes_every_output_through_the_harness(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._tree(d, _smoke_ir(), RANK_SID)
            self._build(d, RANK_SID)
            r = self._run(d, "c0", "c1_xfail")
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            final = json.loads((d / "raw/state_snapshots/c0.json").read_text())
            initial = json.loads((d / "raw/state_snapshots/initial/c0.json").read_text())
            self.assertEqual({"t", "s", "u", "a2", "a3", "a4", "ok", "step"}, set(final))
            self.assertEqual(0.0, final["t"])
            self.assertEqual([[0.0, 1.0], [2.0, 3.0]], initial["a2"])
            self.assertEqual([[9.0, 1.0], [2.0, 3.0]], final["a2"])
            self.assertEqual(1.5, initial["s"])
            self.assertEqual(1.0, final["step"])
            # The refusing case's capture holds what it left, and it committed no step.
            refused = json.loads((d / "raw/state_snapshots/c1_xfail.json").read_text())
            self.assertEqual((0.0, 0.0, 1.5), (refused["ok"], refused["step"], refused["s"]))
            # Nothing of the retired judging half is written (issue #582).
            self.assertFalse((d / "diagnostics.json").exists())
            self.assertFalse((d / "raw/metrics_basis.json").exists())
            perf = json.loads((d / "perf.json").read_text())
            self.assertEqual({"c1_xfail", "gpu", 1, 3}, {
                perf["case_id"], perf["target"], perf["steps"],
                perf["parallelism"]["threads_per_rank"]})
            self.assertEqual(16, perf["cells_updated"])

    def test_the_runner_hands_the_imposed_block_size_to_the_node_s_sources(self) -> None:
        """Issue #512 (PR-4c): the runner sets `spec2science::launch_block_size` from the harness's
        `__block_size` before the first case, so a node's operation already reads the block the
        host imposed through `SPEC2SCIENCE_CUDA_BLOCK`; the node's sources never name the harness."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._tree(d, _smoke_ir(), RANK_SID)
            self._build(d, RANK_SID)
            for env, block in (({}, 256), ({"SPEC2SCIENCE_CUDA_BLOCK": "64"}, 64)):
                with self.subTest(block=block):
                    r = self._run(d, "c0", "c1_xfail", env=env)
                    self.assertEqual(0, r.returncode, r.stdout + r.stderr)
                    final = json.loads((d / "raw/state_snapshots/c0.json").read_text())
                    self.assertEqual(3.0 + block, final["s"])
                    perf = json.loads((d / "perf.json").read_text())
                    self.assertEqual(block, perf["parallelism"]["block_size"])

    #: A destructor and an exit handler of the model's that rewrite a run output.
    _FORGER = textwrap.dedent("""\
        #include <cstdlib>
        #include <fstream>
        namespace {
        void forge() { std::ofstream("perf.json") << "{\\"forged\\": 1}"; }
        struct Forger {
          Forger() { std::atexit(forge); }
          ~Forger() { forge(); }
        } forger;
        }  // namespace
        """)

    def test_no_leaf_code_runs_after_the_harness_writes(self) -> None:
        """Round 3 of this change's review: a leaf source's namespace-scope destructor and exit
        handler ran after `main` returned — after the harness wrote the outputs — and rewrote
        them. The runner ends every exit with `std::_Exit`, so neither runs. (The source gates
        refuse the file I/O this uses too; this pins the runner's half on its own.)"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._tree(d, _smoke_ir(), RANK_SID, _smoke_model() + self._FORGER)
            self._build(d, RANK_SID)
            r = self._run(d, "c0", "c1_xfail")
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            self.assertNotIn("forged", (d / "perf.json").read_text())
            self.assertIn("steps", json.loads((d / "perf.json").read_text()))

    def test_an_operation_cannot_replace_a_snapshot(self) -> None:
        """Round 5 of this change's review, on the driver: the final capture is written after the
        last call of the case, so what an operation wrote at that path does not survive. (The
        source gates refuse the file I/O too; this pins the runner's half on its own.)"""
        model = _smoke_model().replace(
            "  ok = mode == 0;\n",
            '  std::ofstream("raw/state_snapshots/c0.json") << "{}";\n  ok = mode == 0;\n').replace(
            f'#include "{RANK_SID}_model.cuh"\n', f'#include "{RANK_SID}_model.cuh"\n#include <fstream>\n')
        self.assertIn("std::ofstream", model)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._tree(d, _smoke_ir(), RANK_SID, model)
            self._build(d, RANK_SID)
            r = self._run(d, "c0")
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            self.assertIn("a3", json.loads((d / "raw/state_snapshots/c0.json").read_text()))

    def test_a_case_with_no_file_stops_the_run_with_no_leaf_code_after_it(self) -> None:
        """...through `finish`, so no leaf destructor runs on this exit either. It is what an
        array the case file does not hold reaches too (`test_driver_render`)."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._tree(d, _smoke_ir(), RANK_SID, _smoke_model() + self._FORGER)
            self._build(d, RANK_SID)
            r = self._run(d, "no_such_case")
            self.assertEqual(2, r.returncode)
            self.assertIn("no readable case file for case no_such_case", r.stderr)
            self.assertFalse((d / "perf.json").exists())

    def test_a_missing_case_list_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._tree(d, _smoke_ir(), RANK_SID, _smoke_model() + self._FORGER)
            self._build(d, RANK_SID)
            r = subprocess.run(["./runner"], cwd=d, capture_output=True, text=True, check=False)
            self.assertEqual(1, r.returncode)
            self.assertIn("--cases <spec> <case_id>... required", r.stderr)
            self.assertFalse((d / "perf.json").exists())


class HostVerdictRunnerSurfaceTest(unittest.TestCase):
    """Issue #582, the counterpart of `test_fortran_runner.HostVerdictRunnerSurfaceTest`: the
    rendered runner judges nothing, names only the harness operations it calls, and captures
    every schema variable around the operation."""

    RETIRED = (*ft_tests.HostVerdictRunnerSurfaceTest.RETIRED,
               *ft_tests.HostVerdictRunnerSurfaceTest.RETIRED_CHECKS)

    def _each(self):
        for label, ir, sid in (("smoke", _smoke_ir(), RANK_SID),
                               ("boundary", _boundary_ir(), BOUNDARY_SID),
                               ("loop", loop_ir(0), "drv_toy"), ("init", init_ir(0), "drv_toy"),
                               ("procedure", procedure_ir(), "drv_toy")):
            with self.subTest(label):
                yield ir, sid, _render(ir, sid)

    def test_no_retired_callback_writer_or_record_is_named(self) -> None:
        for _ir, _sid, text in self._each():
            for gone in self.RETIRED:
                self.assertNotIn(gone, text)

    def test_the_harness_names_it_spells_are_the_ones_it_uses(self) -> None:
        for ir, _sid, text in self._each():
            ops = {f"harness_cpp_gpu__{op}" for op in cpp_runner._used_harness_ops(ir)}
            spelled = set(re.findall(r"harness_cpp_gpu__\w+", text))
            self.assertEqual(ops | {"harness_cpp_gpu__h_named"}, spelled)
            # every declared call has an argument list at its use, not just a mention
            for op in ops:
                self.assertIn(f"{op}(", text)

    def test_every_schema_variable_is_captured_at_both_points(self) -> None:
        for ir, _sid, text in self._each():
            variables = [v["name"] for v in ir["io_contract"]["raw_requirements"][
                "required_evidence"][0]["schema"]["variables"]]
            capture = text[text.index("std::vector<Named> capture_state("):
                           text.index("int main(")]
            for name in variables:
                self.assertIn(f'__box("{name}", ', capture)
                self.assertIn(f"st_{name}", capture)
            self.assertEqual(1, text.count('write_capture("initial/" + cid);'))
            self.assertEqual(1, text.count("write_capture(cid);"))

    def test_write_perf_is_called_once_after_the_case_loop(self) -> None:
        for _ir, _sid, text in self._each():
            self.assertEqual(1, text.count("harness_cpp_gpu__write_perf("))
            self.assertLess(text.rindex("steps_total += step_c;"),
                            text.index("harness_cpp_gpu__write_perf("))

    def test_the_runner_reads_neither_the_tests_nor_the_predicates(self) -> None:
        for ir, sid, text in self._each():
            changed = copy.deepcopy(ir)
            changed["io_contract"].pop("tests", None)
            changed["io_contract"].pop("predicates", None)
            self.assertEqual(text, _render(changed, sid))


class SignaturesRoundTripTest(unittest.TestCase):
    def test_the_harness_stub_satisfies_the_section_5_1_source_pin(self) -> None:
        """The stub is a faithful harness: its definitions carry exactly the pinned atoms."""
        ops, _types, _ifaces, errors = cpp_signatures._stanzas(
            cpp_source.cpp_decls.read(cpp_header.render(HARNESS, _harness_public_api(), spec_kind="infrastructure")
                                      + _harness_stub()), (f"{HARNESS}_model",))
        self.assertEqual([], errors)
        self.assertLessEqual({s["symbol"] for s in _harness_signatures()
                              if "args" in s["signature"]}, set(ops))


if __name__ == "__main__":
    unittest.main()
