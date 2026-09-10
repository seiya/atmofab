#!/usr/bin/env python3
"""M-C: Z2 pure-function CodegenBundle producer.

Covers the host side of the pure `generate.generate` channel added across
`tools/workflow_conductor.py` (the producer loop, bundle validation + assembly preflight, the
bundle-derived Makefile, bundle_meta), `tools/orchestration_runtime.py` (the terminal-payload
carve-out + the cold-repair prompt contract), `tools/validate_pipeline_semantics.py` (the
post_generate bundle re-validation + the sweep output_refs mirror), and `tools/run_workflow.py`
(the generate-executor surface — since M-F the executor is hardcoded pure).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("ATMOFAB_DEP_READINESS_ALLOW_PERSISTED_FALLBACK", "1")

import tools.codegen_bundle as cb
import tools.orchestration_runtime as ort
import tools.workflow_conductor as wc
import tools.validate_pipeline_semantics as vps
from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION
from tools.tests.llm_samples import sample_config_with as _cfg

_NODE = "problem/shallow_water2d@0.3.0"
_SAFE = wc.node_key_safe(_NODE)
_SPEC_ID = "shallow_water2d"
_HARNESS = "infrastructure/harness_fortran_cpu@0.7.0"
_HARNESS_SPEC_ID = "harness_fortran_cpu"
_SPEC_PATH = "spec/problem/ocean/shallow_water2d"

def _runner_text() -> str:
    from tools.backends.language.fortran.runner import render_runner
    return render_runner(_node_ir(), _SPEC_ID, _HARNESS_SPEC_ID)


def cb_runner_imports(runner_text: str, spec_id: str) -> tuple[str, ...]:
    """The names a rendered runner actually imports — a TEST-only read of the runner, kept here to
    assert that the required ABI is deliberately WIDER than it (production must not key off this;
    `Generate.static` requires all ten regardless)."""
    out: list[str] = []
    body, seen = "", False
    for raw in runner_text.splitlines():
        line = raw.split("!", 1)[0].strip()
        if not seen:
            m = re.match(rf"(?i)^use\s+{re.escape(spec_id)}_checks\s*,\s*only\s*:\s*(.*)$", line)
            if not m:
                continue
            seen, line = True, m.group(1).strip()
        else:
            line = line.lstrip("&").strip()
        cont = line.endswith("&")
        body += line[:-1] if cont else line
        if not cont:
            break
    for tok in body.split(","):
        tok = tok.strip()
        if re.fullmatch(r"[A-Za-z]\w*", tok):
            out.append(tok)
    return tuple(out)


def _checks_symbols() -> tuple[str, ...]:
    """The fixed checks ABI — required in FULL of every M3c node, not the subset this node's
    runner imports (`Generate.static` checks all ten). Imported from the renderer, the ABI's
    authority, rather than hand-typed here."""
    from tools.backends.language.fortran.runner import CHECKS_PUBLIC_NAMES
    return CHECKS_PUBLIC_NAMES


def _checks_content(*, omit: str = "", as_function: str = "", unexported: str = "") -> str:
    """A checks module publishing the fixed ABI, in the certified idiom (a bare `private` default
    plus an explicit `public ::` list — authoring rule 1). `omit` drops a name entirely,
    `as_function` defines one as a FUNCTION (the two shapes the sw2d P-arm emitted), and
    `unexported` defines one but leaves it off the export list.

    Since the per-id checks ABI (pure-8), the module authors no check id — the runner supplies each
    id as a literal actual — so there is no id-literal-presence layer to satisfy here."""
    syms = [s for s in _checks_symbols() if s != omit]
    body = [f"module {_SPEC_ID}_checks", "  private"]
    body += [f"  public :: {s}" for s in syms if s != unexported]
    body.append("contains")
    for sym in syms:
        if sym == as_function:
            body += [f"  function {sym}() result(r)", f"  end function {sym}"]
        else:
            body += [f"  subroutine {sym}()", f"  end subroutine {sym}"]
    body.append("end module")
    return "\n".join(body) + "\n"


def _valid_bundle() -> dict:
    return {
        "bundle_schema_version": "1.0.0",
        "optimization_unit": {"members": [_NODE]},
        "files": [
            {"logical_path": f"{_SPEC_ID}_model.f90", "role": "model", "language": "fortran",
             "member_node_key": _NODE, "content": f"module {_SPEC_ID}_model\nend module\n",
             "modules": [f"{_SPEC_ID}_model"]},
            {"logical_path": f"{_SPEC_ID}_checks.f90", "role": "checks", "language": "fortran",
             "member_node_key": _NODE, "content": _checks_content(),
             "modules": [f"{_SPEC_ID}_checks"]},
        ],
        "entrypoints": [
            {"symbol": "sw_update", "kind": "operation", "node_key": _NODE,
             "defined_in": f"{_SPEC_ID}_model.f90", "module": f"{_SPEC_ID}_model"},
            {"symbol": "case_run", "kind": "checks_interface", "node_key": _NODE,
             "defined_in": f"{_SPEC_ID}_checks.f90", "module": f"{_SPEC_ID}_checks"},
        ],
        "target_lowering_plan": {"precision": {"real_kind": "real64"}, "state_residency": "host"},
        "capability_requirements": ["sync_single_case@1"],
    }


def _node_ir(state_vars=("h", "u", "v")) -> dict:
    """A minimal M3c problem IR that `render_runner` accepts — so the fixture's runner can be
    produced by the real renderer instead of hand-typed (a hand-typed runner would let the ABI
    layer pass against a shape the renderer never emits)."""
    return {
        "meta": {"spec_id": _SPEC_ID, "spec_kind": "problem"},
        "impl_defaults": {
            "toolchain": {"language": "fortran", "standard": "f2008", "build_system": "make"},
            "target": {"backend": "cpu"},
        },
        # Canonical shape: state_variables is a list of OBJECTS ({name, shape_expr}), NOT bare
        # strings — the shape real specs emit (a string-list masks the name-extraction path).
        "algorithm": {"state_variables": [{"name": v, "shape_expr": "[nx]"} for v in state_vars]},
        "dependency": {"direct_deps": [{"node_key": _HARNESS}]},
        "case": {"test_case_set": [{"case_id": "c1"}]},
        "io_contract": {
            "raw_requirements": {"required_evidence": [
                {"artifact": "state_snapshots", "schema": {
                    "variables": [{"name": "h", "shape_expr": "[4, 4]"}],
                    "time_variable": "t"}},
            ]},
            "test_evidence_requirements": [{"test_id": "c1", "required_raw_variables": ["h"]}],
            "diagnostics_contract": {
                "checks": [{"id": "mass"}],
                "metrics": ["error.l2"],
                "verdict": {"required": True, "fields": ["overall", "failed_checks"]},
            },
            "test_predicates": [
                {"test_id": "c1", "expected_outcome": "pass", "target_cases": ["c1"]}],
        },
    }


def _write_node(repo: Path, *, ir_id="sw_20260715_001", source_id="src_20260715_001",
                state_vars=("h", "u", "v"), stage_runner=True) -> wc.NodeRefs:
    """Write a minimal M3c IR + dependency-graph sidecar + tests.md for the node, and stage the
    host-rendered runner the way `run_phase` does before any generate substep runs."""
    ir_dir = repo / "workspace" / "ir" / _SAFE / ir_id
    ir_dir.mkdir(parents=True, exist_ok=True)
    ir = _node_ir(state_vars)
    import yaml
    (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(ir), encoding="utf-8")
    sidecar = {
        "all_nodes": [
            {"node_key": _NODE, "topo_level": 1, "direct_deps": [{"node_key": _HARNESS}]},
            {"node_key": _HARNESS, "topo_level": 0, "direct_deps": []},
        ],
    }
    (ir_dir / "dependency_graph.json").write_text(json.dumps(sidecar), encoding="utf-8")
    spec_dir = repo / _SPEC_PATH
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "tests.md").write_text("- test: conserves mass\n", encoding="utf-8")
    (spec_dir / "controlled_spec.md").write_text(
        "## 5 Algorithm\nhydrostatic reconstruction: h_star = max(0, eta - z_b)\n",
        encoding="utf-8")
    refs = wc.NodeRefs(node_key=_NODE, spec_path=_SPEC_PATH, ir_id=ir_id,
                       pipeline_id="sw_20260715_001", source_id=source_id)
    if stage_runner:
        src_dir = repo / refs.source_dir() / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / f"{_SPEC_ID}_runner.f90").write_text(_runner_text(), encoding="utf-8")
    return refs


class _PureFakeConductor(wc.Conductor):
    """Conductor with the runtime CLI and leaf spawn stubbed, but the pure host-side logic
    (context assembly, bundle validation, graph derivation, artifact writes) real."""

    envelopes: list[str] = []

    def _write_launch_input_evidence(self, filename, payload):  # type: ignore[override]
        # In-memory: this fixture's repo_root is a shared throwaway path, and the real
        # write path is pinned separately in test_workflow_conductor.py.
        # Round-trip through the encoder the real writer uses — see the note on
        # `_FakeConductor._write_launch_input_evidence` in test_workflow_conductor.py.
        store = self.__dict__.setdefault("evidence", {})
        store[filename] = json.loads(json.dumps(payload, ensure_ascii=True))
        return f"workspace/orchestrations/{self.orchestration_id}/launches/{filename}"

    def _resolve_evidence(self, rel):
        return self.__dict__.setdefault("evidence", {})[rel.rsplit("/", 1)[-1]]

    def runtime(self, args, *, input=None):  # type: ignore[override]
        sub = args[0]
        self.calls = getattr(self, "calls", [])
        captured: dict = {}
        # The payload files carry what the inline flags used to; capture under the
        # inline key so assertions stay on one stable name.
        for flag, inline in (("--request-json-file", "--request-json"),
                             ("--agent-run-json-file", "--agent-run-json")):
            if flag in args:
                captured[inline] = self._resolve_evidence(args[args.index(flag) + 1])
        if "--reply-from-stdin" in args:
            captured["--reply-text"] = input
        for flag in ("--agent-run-json", "--request-json", "--run-ids", "--reason"):
            if flag in args and flag != "--run-ids":
                captured[flag] = json.loads(args[args.index(flag) + 1]) \
                    if flag.endswith("-json") else args[args.index(flag) + 1]
        self.calls.append((sub, captured))
        if sub == "record-launch":
            return {"launch_prompt_text": "PROMPT"}
        return {}

    def new_agent_run_id(self):  # type: ignore[override]
        self._n = getattr(self, "_n", 0) + 1
        return f"child-{self._n}"

    def spawn_leaf(self, prompt_text, child_env, entry=None, **kwargs):  # type: ignore[override]
        self._spawn = getattr(self, "_spawn", 0)
        env = self.envelopes[min(self._spawn, len(self.envelopes) - 1)]
        self._spawn += 1
        return wc.ProcResult(0, env, "")

    def read_parent_return_token(self, child_arid):  # type: ignore[override]
        return "rtok"

    def _claude_session_resumable(self, arid, **kw):  # type: ignore[override]
        return True

    # `--wait-usage-reset` asks the host CLI for the reset instant (`claude -p /usage`) before
    # falling back to scraping the dead leaf's stdout. Stubbed for EVERY fake so no test spawns the
    # real backend, and stubbed as a FAILED probe so these loops exercise the scrape fallback — the
    # path they were written against. `usage_probe_calls` is asserted by the wait tests, so removing
    # this override cannot go unnoticed.
    usage_probe_result: tuple = (None, {"outcome": "probe_error", "duration_ms": 0,
                                        "excerpt": "stubbed: no probe in tests"})

    def _run_usage_probe(self, entry=None):  # type: ignore[override]
        self.usage_probe_calls = getattr(self, "usage_probe_calls", 0) + 1
        return self.usage_probe_result


def _envelope(bundle_or_text, *, model="claude-opus-4-8", is_error=False) -> str:
    result = bundle_or_text if isinstance(bundle_or_text, str) else json.dumps(bundle_or_text)
    return json.dumps({"result": result, "is_error": is_error, "model": model,
                       "usage": {"output_tokens": 10}, "session_id": "s"})


def _conductor(repo: Path) -> _PureFakeConductor:
    (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
    return _PureFakeConductor(
        repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
        llm_config=_cfg("claude"), env={})


# ======================================================================================
# _pure_bundle_violations: clean bundle + each failure category
# ======================================================================================
class PureBundleViolationsTests(unittest.TestCase):
    def _c_refs(self):
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        return _conductor(repo), refs

    def tearDown(self) -> None:
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_clean_bundle_has_no_violations(self) -> None:
        c, refs = self._c_refs()
        self.assertIsNone(c._pure_bundle_violations(refs, _valid_bundle()))

    def test_schema_violation(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        del bad["capability_requirements"]
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_schema_violation")

    def test_shape_unsupported_multinode(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["optimization_unit"]["members"] = [_NODE, "problem/other@0.1.0"]
        cat, _ = c._pure_bundle_violations(refs, bad)
        # A second member with no files is a schema-invariant failure first; either way it is
        # not accepted. Assert it is rejected with a bundle category.
        self.assertIn(cat, ("bundle_shape_unsupported", "bundle_schema_violation"))

    def test_capability_unsatisfied(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["capability_requirements"] = ["batched_cases@1"]
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_capability_unsatisfied")

    def test_state_binding_mismatch(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["modules"] = [f"{_SPEC_ID}_checks"]
        bad["state_bindings"] = [{
            "node_key": _NODE, "state_variable": "not_a_state", "storage_symbol": "q_storage",
            "module": f"{_SPEC_ID}_checks", "capture": "checks_getter", "capability": None}]
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_state_binding_mismatch")

    def test_state_binding_on_canonical_ir_object_state_var_accepted(self) -> None:
        # Codex P2 (finding 1): with the canonical OBJECT-shaped state_variables, a binding on a
        # REAL declared state var ("h") must be accepted. A comprehension that kept only str
        # entries would leave ir_state_vars empty and wrongly reject this as a mismatch.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        ok["files"][1]["modules"] = [f"{_SPEC_ID}_checks"]
        ok["state_bindings"] = [{
            "node_key": _NODE, "state_variable": "h", "storage_symbol": "q_storage",
            "module": f"{_SPEC_ID}_checks", "capture": "checks_getter", "capability": None}]
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_m3c_name_match_is_case_sensitive_like_the_filesystem(self) -> None:
        # `logical_path` becomes a FILENAME, and Generate.static opens `<spec_id>_checks.f90`
        # verbatim on a case-sensitive filesystem. Casefolding accepted `Shallow_Water2d_Checks.f90`
        # — which lints and compiles fine (Fortran resolves `use` by module name, never by
        # filename) — and then Generate.static rejected it on the name: accepted here, rejected
        # there, i.e. a phase reopen, which is what this layer exists to prevent.
        c, refs = self._c_refs()
        for role, mixed in (("checks", "Shallow_Water2d_Checks.f90"),
                            ("model", "Shallow_Water2d_Model.f90")):
            bad = _valid_bundle()
            entry = next(f for f in bad["files"] if f["role"] == role)
            was = entry["logical_path"]
            entry["logical_path"] = mixed
            for e in bad["entrypoints"]:
                if e["defined_in"] == was:
                    e["defined_in"] = mixed
            self.assertEqual(cb.validate_bundle(bad), [], "the shape must be schema-legal")
            cat, _ = c._pure_bundle_violations(refs, bad)
            self.assertEqual(cat, "bundle_assembly_collision", role)

    def test_m3c_module_name_match_stays_case_insensitive(self) -> None:
        # The mirror image: a Fortran identifier IS case-insensitive, so a module declared
        # `Shallow_Water2D_Checks` resolves for the runner's `use` and must be accepted.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        ok["files"][1]["modules"] = [f"{_SPEC_ID}_CHECKS"]
        self.assertIsNone(cb.m3c_literal_name_violation(ok, _SPEC_ID))

    def test_m3c_name_violation(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][0]["logical_path"] = "wrong_model.f90"
        bad["entrypoints"][0]["defined_in"] = "wrong_model.f90"
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_assembly_collision")

    def test_accepted_bundle_also_passes_the_real_generate_static_gate(self) -> None:
        # Codex P1, the regression that matters: this layer accepting a bundle that
        # `Generate.static` then rejects reopens the phase — the failure it exists to prevent.
        # Drive the REAL gate, not a restatement of it: the ABI is fixed at ten for every node,
        # and requiring only the subset THIS node's runner imports (6 of 10 here) was accepted
        # here and rejected there.
        import tempfile as _tf
        c, refs = self._c_refs()
        bundle = _valid_bundle()
        self.assertIsNone(c._pure_bundle_violations(refs, bundle))
        checks = next(f for f in bundle["files"] if f["role"] == "checks")
        with _tf.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / checks["logical_path"]).write_text(checks["content"], encoding="utf-8")
            execution = SimpleNamespace(node_key=_NODE)
            v: list[str] = []
            vps._validate_checks_source_files(execution, "fortran", src, [], v)
            self.assertEqual([s for s in v if "publish the fixed ABI" in s], [], v)

    def test_a_bundle_language_with_no_runner_render_capability_is_refused(self) -> None:
        """The refusal branch of the ABI gate, driven — it is corpus-dependent, not unreachable.

        The two vps call sites of this seam take their language from `_ir_m3c_language`, which
        has already required the value to provide `runner_render`, so their refusal really is
        dead code. THIS one does not: the language is read off the bundle FILE entry, which the
        bundle validator constrains only to `LANGUAGES` — the languages whose backend carries a
        `bundle` interface. That set is computed independently of who declares `runner_render`,
        so a language can legally reach here with no checks ABI to be held to. Today the two
        sets happen to be equal (`('fortran',)`), which is why deleting the branch survives the
        suite; a second language backend with a bundle and no renderer separates them, and then
        a waiver here would accept a checks module against an ABI nobody defined.
        """
        from unittest import mock

        import tools.codegen_bundle as cb
        from tools.backends import registry as backend_registry

        bundle = _valid_bundle()
        checks = next(f for f in bundle["files"] if f["role"] == "checks")
        checks["language"] = "zz_bundle_only"
        record = backend_registry.Backend(
            "language", "zz_bundle_only", "tools.backends.language.fortran",
            core_provides=frozenset({"control_file"}))
        with mock.patch.dict(
                backend_registry._BACKENDS,
                {("language", "zz_bundle_only"): record}):
            self.assertFalse(
                backend_registry.provides("language", "zz_bundle_only", "runner_render"))
            violation = cb.m3c_checks_abi_violation(bundle, _SPEC_ID)
        self.assertIsNotNone(violation, "a language with no checks ABI must not be waived")
        self.assertIn("zz_bundle_only", violation)
        # The registry's own clause, carried rather than re-worded.
        self.assertIn("runner_render", violation)

    def test_runner_imported_subset_is_not_the_required_set(self) -> None:
        # Pins the direction of the Codex P1 fix: the runner here imports 6 of the 10, and a
        # bundle publishing only those 6 must be REJECTED (Generate.static wants all ten).
        c, refs = self._c_refs()
        from tools.backends.language.fortran.runner import CHECKS_PUBLIC_NAMES
        imported = cb_runner_imports(_runner_text(), _SPEC_ID)
        self.assertTrue(set(imported) < set(CHECKS_PUBLIC_NAMES), imported)
        bad = _valid_bundle()
        syms = list(imported)
        bad["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n  private\n"
            + "".join(f"  public :: {s}\n" for s in syms)
            + "contains\n"
            + "".join(f"  subroutine {s}()\n  end subroutine {s}\n" for s in syms)
            + "end module\n")
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        for absent in set(CHECKS_PUBLIC_NAMES) - set(imported):
            self.assertIn(absent, findings)

    def test_checks_abi_missing_symbol(self) -> None:
        # Z2 defect D, shape 1 (sw2d src_003): the checks module omits names the runner imports
        # -> Generate.syntax "Symbol 'x' not found in module". Now a bounded in-loop repair.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content(omit="checks_compute")
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("checks_compute", findings)

    def test_checks_abi_quoted_type_function_form_rejected(self) -> None:
        # Codex review: a legal type-spec can hold a quote in its parens —
        # `character(kind=kind('a')) function metric_compute()`. Excluding quotes from the
        # proc-header prefix made that header unmatchable, so the function was published-but-not-
        # defined and the gate ACCEPTED it; the runner then `call`s it and Generate.syntax fails —
        # the phase reopen this gate exists to prevent. Matching a string-masked line catches it.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        syms = _checks_symbols()
        bad["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n  private\n"
            + "".join(f"  public :: {s}\n" for s in syms) + "contains\n"
            + "".join(f"  subroutine {s}()\n  end subroutine {s}\n"
                      for s in syms if s != "metric_compute")
            + "  character(kind=kind('a')) function metric_compute()\n"
            "    metric_compute = 'x'\n  end function metric_compute\n"
            "end module\n")
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("metric_compute", findings)
        self.assertIn("FUNCTION", findings)

    def test_checks_abi_function_form_rejected(self) -> None:
        # Z2 defect D, shape 2 (sw2d src_004): the name EXISTS but is authored as a FUNCTION,
        # while the runner `call`s it -> "has a type, which is not consistent with the CALL".
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content(as_function="metric_compute")
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("metric_compute", findings)
        self.assertIn("FUNCTION", findings)

    def test_checks_abi_accepts_names_not_defined_locally(self) -> None:
        # Review finding: a published ABI name need not be defined in this module's text to be
        # callable — `use`-association, a generic `interface`, and a submodule all compile, LINK
        # and `call` fine (verified with gfortran). Demanding a local `subroutine` header rejected
        # those, with findings telling the producer to write what it had already written: a repair
        # loop with no exit, i.e. this defect's own failure mode on a LEGAL bundle. Only positive
        # evidence of the wrong kind (a local FUNCTION) may reject.
        c, refs = self._c_refs()
        syms = _checks_symbols()
        pubs = "".join(f"  public :: {s}\n" for s in syms)
        defs = "".join(f"  subroutine {s}()\n  end subroutine {s}\n"
                       for s in syms if s != "case_setup")
        ok = _valid_bundle()
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks_impl\n  public\ncontains\n"
            "  subroutine case_setup()\n  end subroutine case_setup\n"
            f"end module {_SPEC_ID}_checks_impl\n"
            f"module {_SPEC_ID}_checks\n"
            f"  use {_SPEC_ID}_checks_impl, only: case_setup\n  private\n{pubs}contains\n"
            + defs
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_defined_but_unexported_rejected(self) -> None:
        # Review finding: the certified idiom is a bare `private` + explicit `public ::` list, so
        # a name defined but left off that list is INVISIBLE to the runner and fails
        # Generate.syntax with the same "Symbol not found in module" as an omitted one. Checking
        # definition alone would fail open on half of the shape this layer exists to catch.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content(unexported="checks_compute")
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("checks_compute", findings)
        self.assertIn("not published", findings)

    def test_checks_abi_explicit_private_name_rejected(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content().replace(
            "  private\n", "  private :: get_time\n", 1)
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("get_time", findings)

    def test_checks_abi_accepts_default_public_module(self) -> None:
        # No bare `private` => Fortran's own default is public => nothing to export explicitly.
        # The export check must not invent a requirement the language does not impose.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n"
            + "".join(f"  subroutine {s}()\n  end subroutine\n" for s in _checks_symbols())
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_accepts_leading_ampersand_continuation(self) -> None:
        # Free-form Fortran allows an OPTIONAL leading `&` on a continuation line (confirmed
        # legal with `gfortran -fsyntax-only -std=f2008`). Fused onto the next name it would be
        # dropped by the identifier filter and reject a module that DID export it — and the
        # findings text would tell the leaf to do what it had already done, so the repair loop
        # could only thrash. Latent today (every certified module wraps with a trailing `&`
        # only), but the checks module is leaf-authored and continuation style varies.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        syms = _checks_symbols()
        wrapped = "  public :: " + ", &\n       &  ".join(syms) + "\n"
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n  private\n" + wrapped
            + "contains\n"
            + "".join(f"  subroutine {s}()\n  end subroutine\n" for s in syms)
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_derived_type_private_is_not_the_module_default(self) -> None:
        # False-positive guard on a fail-closed gate: a bare `private` inside a derived type sets
        # that TYPE's component accessibility. Reading it as the module default would demand a
        # `public ::` list from a legal default-public module and reject it.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n"
            "  type :: bucket\n    private\n    integer :: n\n  end type bucket\n"
            "contains\n"
            + "".join(f"  subroutine {s}()\n  end subroutine\n" for s in _checks_symbols())
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_declaration_attribute_is_not_an_accessibility_statement(self) -> None:
        # `integer, private :: n` is an attribute on a declaration, not a module default, and
        # `private :: n` on an unrelated helper must not touch the ABI names.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n"
            "  integer, private :: n\n  private :: helper\n"
            "contains\n"
            + "".join(f"  subroutine {s}()\n  end subroutine\n" for s in _checks_symbols())
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_accepts_wrapped_export_list(self) -> None:
        # The certified idiom wraps its `public ::` list with `&` continuations.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        syms = _checks_symbols()
        wrapped = "  public :: " + ", &\n    ".join(syms) + "\n"
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n  private\n" + wrapped
            + "".join(f"  subroutine {s}()\n  end subroutine\n" for s in syms)
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_end_subroutine_and_comments_do_not_satisfy(self) -> None:
        # Fail-open guard: every real module carries `end subroutine <name>` for each subroutine,
        # so an unanchored `subroutine <name>` search would accept the FUNCTION form this layer
        # exists to reject. A mention in a comment must not satisfy it either.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n"
            + "".join(f"subroutine {s}()\nend subroutine {s}\n"
                      for s in _checks_symbols() if s != "metric_compute")
            + "! the runner also calls subroutine metric_compute\n"
            + "function metric_compute() result(r)\nend subroutine metric_compute\n"
            + "end module\n")
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("metric_compute", findings)

    def test_checks_abi_accepts_prefixed_subroutine_forms(self) -> None:
        # ...while the anchoring must not reject a legal `pure subroutine foo()` definition.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n"
            + "".join(f"  pure subroutine {s}()\n  end subroutine {s}\n"
                      for s in _checks_symbols())
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_ignores_a_decoy_sibling_checks_module(self) -> None:
        # Codex review: a bundle may legally carry more than one checks-role file. Searching them
        # all lets a sibling module's procedures vouch for names the runner — which imports
        # <spec_id>_checks alone — can never see: accepted here, then dead at Generate.syntax.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = f"module {_SPEC_ID}_checks\nend module\n"
        bad["files"].append({
            "logical_path": "decoy_checks.f90", "role": "checks", "language": "fortran",
            "member_node_key": _NODE,
            "content": _checks_content().replace(f"{_SPEC_ID}_checks", "decoy_checks"),
            "modules": ["decoy_checks"]})
        self.assertEqual(cb.validate_bundle(bad), [])  # the shape really is schema-legal
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")

    def test_checks_abi_ignores_a_decoy_second_module_in_the_same_file(self) -> None:
        # Same class, one level down: file scoping alone would still be fooled, so the check is
        # scoped to the MODULE the runner imports.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\nend module\n"
            + _checks_content().replace(f"{_SPEC_ID}_checks", "decoy_checks"))
        bad["files"][1]["modules"] = [f"{_SPEC_ID}_checks", "decoy_checks"]
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")

    def test_checks_abi_extra_checks_file_does_not_mask_a_real_violation(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content(omit="checks_compute")
        bad["files"].append({
            "logical_path": "helper_checks.f90", "role": "checks", "language": "fortran",
            "member_node_key": _NODE,
            "content": _checks_content().replace(f"{_SPEC_ID}_checks", "helper_checks"),
            "modules": ["helper_checks"]})
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("checks_compute", findings)

    def test_checks_abi_fail_closed_when_declared_module_is_not_defined(self) -> None:
        # `modules[]` is leaf-declared; if the content defines no such module the runner's import
        # cannot resolve, so an unverifiable ABI must not be an accepted one.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = "! no module here\n"
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")

    def test_state_binding_fail_closed_when_ir_declares_no_state(self) -> None:
        # Review fix: an EMPTY declared state set must REJECT any binding (not accept all).
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo, state_vars=())
        c = _conductor(repo)
        bad = _valid_bundle()
        bad["state_bindings"] = [{
            "node_key": _NODE, "state_variable": "h", "storage_symbol": "q_storage",
            "module": f"{_SPEC_ID}_checks", "capture": "checks_getter", "capability": None}]
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_state_binding_mismatch")


class PureContextRunnerInjectionTests(unittest.TestCase):
    """Z2 defect D: the tool-less leaf can reach the checks ABI ONLY through the injected
    runner, so the injection itself is the fix and is pinned here."""

    def test_runner_document_is_the_staged_file_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            ctx = _conductor(repo)._build_pure_context(refs)
            staged = (repo / refs.source_dir() / "src"
                      / f"{_SPEC_ID}_runner.f90").read_text(encoding="utf-8")
            self.assertEqual(ctx["runner_document"], staged)
            # The ABI the leaf must author against is actually reachable in what it is shown.
            self.assertIn(f"use {_SPEC_ID}_checks, only:", ctx["runner_document"])

    def test_controlled_spec_is_not_in_producer_context(self) -> None:
        # pure-10: the pure-5 interim carve-out was removed — the producer is spec-blind again
        # (phase_02 §2-1), so its context carries NO controlled_spec_document. The verify reviewer
        # still reads controlled_spec.md by design (test_pure_leaf_verify.py).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            ctx = _conductor(repo)._build_pure_context(refs)
            self.assertNotIn("controlled_spec_document", ctx)

    def test_non_utf8_runner_raises_the_named_contract_not_a_bare_decode_error(self) -> None:
        # UnicodeDecodeError is a ValueError, not an OSError: catching OSError alone let it escape
        # unnamed. The caller recovers any exception, so this is about the diagnosis an operator
        # reads, not about containment.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            (repo / refs.source_dir() / "src" / f"{_SPEC_ID}_runner.f90").write_bytes(
                b"program p\n  use x_checks, only: \xff\xfe bar\nend program\n")
            with self.assertRaises(RuntimeError) as cm:
                _conductor(repo)._build_pure_context(refs)
            self.assertIn("pure_runner_document_missing", str(cm.exception))

    def test_missing_runner_raises_rather_than_shipping_a_blank_abi(self) -> None:
        # NOT the swallow-to-"" idiom ir/tests use. Measured in issue #169's review: a
        # whitespace-only `pure_context` value never reaches the leaf — the launch
        # validator counts it missing and raises — so degrading only defers the refusal
        # one frame, into `record_launch`, where it escapes the loop's named branch.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo, stage_runner=False)
            with self.assertRaises(RuntimeError) as cm:
                _conductor(repo)._build_pure_context(refs)
            self.assertIn("pure_runner_document_missing", str(cm.exception))


class PureHarnessManifestNarrowingTests(unittest.TestCase):
    """The manifest a pure leaf is SHOWN must be the one it is JUDGED against (Codex review).

    `_build_pure_context` used to inline the FULL `HARNESS_CAPABILITY_MANIFESTS` table while
    `_pure_bundle_violations` negotiated only against the node's own infra dependency. With one
    registered harness the two coincide, so the defect is latent; these tests register a SECOND
    harness to make it live, which is the only way to reach the branch.
    """

    def setUp(self) -> None:
        self._saved = dict(cb.HARNESS_CAPABILITY_MANIFESTS)
        # A second harness providing what the node's own harness does not.
        cb.HARNESS_CAPABILITY_MANIFESTS["infrastructure/harness_gpu_next@0.1.0"] = frozenset(
            {"async_device_resident@1", "state_registration@1"})
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.refs = _write_node(self.repo, state_vars=("h",))
        self.c = _conductor(self.repo)

    def tearDown(self) -> None:
        cb.HARNESS_CAPABILITY_MANIFESTS.clear()
        cb.HARNESS_CAPABILITY_MANIFESTS.update(self._saved)
        self._tmp.cleanup()

    def test_context_shows_only_the_nodes_own_harness(self) -> None:
        shown = json.loads(self.c._build_pure_context(self.refs)["harness_capabilities"])
        self.assertEqual([m["node_key"] for m in shown["manifests"]], [_HARNESS])

    def test_context_hides_another_harnesss_capabilities(self) -> None:
        # The live consequence: the prompt licenses `harness_registration` when the manifest it is
        # shown lists `state_registration@N`. Leaking another harness's token would license a
        # requirement `_pure_bundle_violations` then rejects as `bundle_capability_unsatisfied` —
        # a repair burn on a bundle the leaf could not know was unsatisfiable.
        shown = json.loads(self.c._build_pure_context(self.refs)["harness_capabilities"])
        provided = {t for m in shown["manifests"] for t in m["provides"]}
        self.assertNotIn("state_registration@1", provided)
        self.assertNotIn("async_device_resident@1", provided)
        self.assertEqual(provided, {"sync_single_case@1"})

    def test_context_and_gate_resolve_the_same_harness(self) -> None:
        # The invariant the fix rests on: one resolution, so the two cannot drift.
        ir = {"dependency": {"direct_deps": [{"node_key": _HARNESS}]}}
        self.assertEqual(
            self.c._pure_harness_node_key(ir, self.refs.node_key), _HARNESS)
        shown = json.loads(self.c._build_pure_context(self.refs)["harness_capabilities"])
        from_ir = self.c._pure_harness_node_key(
            wc._read_yaml(self.repo / self.refs.ir_ref / "spec.ir.yaml") or {},
            self.refs.node_key)
        self.assertEqual([m["node_key"] for m in shown["manifests"]], [from_ir])

    def test_narrowing_is_fail_closed_for_an_unresolvable_harness(self) -> None:
        # None / unregistered => EMPTY manifests, mirroring `harness_provided_capabilities`'s
        # fail-closed None. Never "show the whole table because we could not pick one".
        for key in (None, "infrastructure/not_registered@9.9.9"):
            self.assertEqual(
                cb.harness_capability_manifest_document_for(key)["manifests"], [],
                f"narrowing must be empty for {key!r}")

    def test_zero_or_multiple_infra_deps_resolve_to_none(self) -> None:
        nk = self.refs.node_key
        self.assertIsNone(
            self.c._pure_harness_node_key({"dependency": {"direct_deps": []}}, nk))
        self.assertIsNone(self.c._pure_harness_node_key({"dependency": {"direct_deps": [
            {"node_key": _HARNESS},
            {"node_key": "infrastructure/harness_gpu_next@0.1.0"}]}}, nk))

    def test_an_infrastructure_node_negotiates_against_its_own_manifest(self) -> None:
        """Issue #169: on the `harness` shape the leaf IS the harness, so the manifest it is
        shown and judged against is its own — resolved from the NODE_KEY, not from the IR's
        (empty) `direct_deps`, which would otherwise fail closed to None."""
        own = "infrastructure/harness_gpu_next@0.1.0"
        self.assertEqual(
            self.c._pure_harness_node_key({"dependency": {"direct_deps": []}}, own), own)
        # ...and it wins over a dependency read, which cannot apply on this shape.
        self.assertEqual(
            self.c._pure_harness_node_key(
                {"dependency": {"direct_deps": [{"node_key": _HARNESS}]}}, own), own)

    def test_full_document_still_carries_every_manifest(self) -> None:
        # The unnarrowed document is the canonical Z6 shape; narrowing is the leaf's projection
        # of it, not a replacement.
        full = [m["node_key"] for m in cb.harness_capability_manifest_document()["manifests"]]
        self.assertIn(_HARNESS, full)
        self.assertIn("infrastructure/harness_gpu_next@0.1.0", full)


# ======================================================================================
# Bundle-derived Makefile
# ======================================================================================
class PureMakefileTests(unittest.TestCase):
    def test_makefile_compiles_bundle_files_and_runner_and_deps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = _conductor(repo)
            graph = c._build_pure_bundle_graph(refs, _valid_bundle())
            mk = c._render_pure_makefile_from_graph(refs, graph)
            self.assertIn(f"$(OBJDIR)/{_SPEC_ID}_model.o", mk)
            self.assertIn(f"$(OBJDIR)/{_SPEC_ID}_checks.o", mk)
            self.assertIn(f"$(OBJDIR)/{_SPEC_ID}_runner.o", mk)
            # the staged harness dependency compiles too
            self.assertIn("$(OBJDIR)/harness_fortran_cpu_model.o", mk)
            self.assertIn(f"BIN ?= {_SPEC_ID}_runner", mk)
            self.assertIn("test:", mk)


# ======================================================================================
# _run_pure_generate_substep: happy path + bounded repair + exhaustion
# ======================================================================================
class PureProducerSubstepTests(unittest.TestCase):
    def _run(self, envelopes):
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        c = _conductor(repo)
        c.envelopes = envelopes
        oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        return c, refs, oc

    def tearDown(self) -> None:
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_the_requests_host_authorship_stamp_comes_from_the_spec(self) -> None:
        """`makefile_host_authored` / `runner_host_authored` are read BACK off the request
        (`orchestration_runtime._payload_is_m3c_physics` derives the physics-narrowed
        contract-doc set from them), so what stamps them decides whether that derivation is
        the node's truth or a leftover constant.

        Both loops used to write the literal `True, True`. That is correct for the two pairs
        that reach them today — Generate runs pure only on the M3c shape — and it is a seam
        rather than a fact, so it is now the spec's `host_authored_flags`, and the generate /
        compile specs bind `_host_authored_m3c`, which returns that same constant WITH its
        reason attached. This row drives the seam: a spec whose flags answer False produces a
        request that does not stamp, which is what a pure path serving another shape depends
        on. Since issue #169's PR-3 an in-tree node DOES produce it — the `harness` shape binds
        `_node_host_authored_flags`, and `PureHarnessShapeTests` drives that end — so this row
        is now the m3c side of a live pair rather than the only witness of the seam.
        """
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)

        # As shipped: the M3c stamp, and the module helper really does answer for this node.
        c = _conductor(repo)
        c.envelopes = [_envelope(_valid_bundle())]
        c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        request = [cap["--request-json"] for sub, cap in c.calls if sub == "record-launch"][-1]
        self.assertEqual(wc._host_authored_m3c(refs), (True, True))
        self.assertTrue(request.get("runner_host_authored"))

        # Through the seam: a spec that answers False stamps nothing. `build_launch_request`
        # omits the key when the value is False (it is a marker, not a boolean field), so
        # `assertNotIn` is what "the stamp says False" looks like on the wire.
        c2 = _conductor(repo)
        c2.envelopes = [_envelope(_valid_bundle())]
        spec = c2._pure_producer_spec("generate")._replace(
            host_authored_flags=lambda _refs: (False, False))
        c2._run_pure_producer_substep(refs, "generate", "generate", None, (), spec)
        request2 = [cap["--request-json"] for sub, cap in c2.calls if sub == "record-launch"][-1]
        self.assertNotIn("runner_host_authored", request2)

    def test_happy_path_writes_bundle_artifacts_and_empty_output_refs(self) -> None:
        c, refs, oc = self._run([_envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.output_refs, [])
        base = c.repo_root / refs.source_dir()
        self.assertTrue((base / "codegen_bundle.json").exists())
        self.assertTrue((base / "bundle_meta.json").exists())
        self.assertTrue((base / "src" / f"{_SPEC_ID}_model.f90").exists())
        self.assertTrue((base / "src" / f"{_SPEC_ID}_checks.f90").exists())
        self.assertTrue((base / "src" / "Makefile").exists())
        meta = json.loads((base / "bundle_meta.json").read_text())
        self.assertEqual(meta["result"], "pass")
        self.assertEqual(meta["prompt_contract_version"], PURE_PROMPT_CONTRACT_VERSION)
        self.assertEqual(meta["per_attempt"][0]["model"], "claude-opus-4-8")

    def test_the_producer_loop_takes_its_launch_instant_from_the_filesystem(self) -> None:
        """Issue #113's resolver is used by THIS loop too, not only by `run_substep`.

        PROVENANCE, not a decision. `SubstepOutcome.launched_at` was deleted by issue #176 with
        the mini-loop that was its only production consumer, so what the resolver leaves on this
        path is the PROBE ITSELF — an operator's record, under the arid of the row it belongs
        to, of the instant the attempt started. This is that probe's only witness on the pure
        producer loop: without it, a mutation dropping the `_launch_instant` call from
        `_spawn_pure_turn` is silent.

        Round 0's hunk mutation found this loop unwitnessed while its sibling
        `_run_pure_verify_substep` was covered — the two took the instant on lines that read
        identically.
        """
        c, _refs, oc = self._run([_envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        probe = (c.repo_root / "workspace" / "orchestrations" / c.orchestration_id
                 / "agents" / oc.agent_run_id / wc.LAUNCH_INSTANT_PROBE_BASENAME)
        self.assertTrue(probe.exists(), probe)
        self.assertEqual(json.loads(probe.read_text())["agent_run_id"], oc.agent_run_id)

    def test_pure_pass_finalize_payload_satisfies_the_real_summary_validator(self) -> None:
        """The finalize payload a passing pure leaf produces must survive the REAL
        runtime validators.

        REGRESSION (billed E2E, 2026-07-16): every passing pure leaf was rejected by
        `finalize-child` with "agent.summary.txt must include summary or failure
        reason" — the pure executor could never complete a real run. A pure row carries
        an EMPTY `output_refs` by contract, so `_validate_agent_summary_text` falls to
        its "a terminal row with no output_refs must explain itself" branch; the
        conductor passed `result_summary=None` on pass, leaving nothing to satisfy it.

        The whole suite missed it because these tests stub `runtime()`. So drive the
        conductor's ACTUAL captured payload through the REAL validators rather than
        re-asserting the stub. Same anti-mock-green shape as the meta writer↔reader
        contract test.
        """
        from tools.orchestration_runtime import (
            _extract_agent_summary_text, _validate_agent_summary_text,
        )

        c, refs, oc = self._run([_envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        payloads = [cap["--agent-run-json"] for sub, cap in c.calls
                    if sub == "finalize-child" and "--agent-run-json" in cap]
        self.assertTrue(payloads, "finalize-child must have been called with a payload")
        for payload in payloads:
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["output_refs"], [])  # the pure contract
            # The real generator + the real validator — no stub in this path.
            _validate_agent_summary_text(payload, _extract_agent_summary_text(payload))

    def test_a_pure_claude_leafs_envelope_usage_reaches_its_agent_run_row(self) -> None:
        """Issue #47: the pure loops are where the expensive claude leaves run, and their
        `usage` had never been asserted anywhere — the envelope's numbers could be dropped with
        the whole suite green. What the row must carry is the DERIVED `total_tokens` (no
        provider sends one, and `audit_orchestration` accepts a durable row only when it is an
        int) summed across every model in `modelUsage`, plus the envelope's own billed cost."""
        envelope = json.dumps({
            "result": json.dumps(_valid_bundle()), "is_error": False, "session_id": "s",
            "total_cost_usd": 0.065739,
            "usage": {"input_tokens": 2, "output_tokens": 4},
            "modelUsage": {
                "claude-opus-5[1m]": {"inputTokens": 2, "outputTokens": 4,
                                      "cacheReadInputTokens": 14278,
                                      "cacheCreationInputTokens": 5849},
                "claude-haiku-4-5-20251001": {"inputTokens": 6, "outputTokens": 480,
                                              "cacheReadInputTokens": 13000,
                                              "cacheCreationInputTokens": 0}}})
        c, refs, oc = self._run([envelope])
        self.assertEqual(oc.status, "pass")
        row = [cap["--agent-run-json"] for sub, cap in c.calls
               if sub == "finalize-child" and "--agent-run-json" in cap][-1]
        self.assertEqual(row["usage"], {
            "input_tokens": 8, "output_tokens": 484,
            "cache_read_input_tokens": 27278, "cache_creation_input_tokens": 5849,
            "total_tokens": 33619, "usage_source": "cli_result_envelope",
            "cost_usd": 0.065739})
        # The same numbers reach the A/B metrics file, which sums `per_attempt[].usage`.
        meta = json.loads((c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
        self.assertEqual(meta["per_attempt"][0]["usage"], row["usage"])

    def test_a_pure_envelope_with_a_partial_modelusage_drops_the_cost(self) -> None:
        """The pure twin of the agentic cost-suppression pin: `total_cost_usd` is the sum
        across every model, so a token count that covers only some of them must not be paired
        with it — that is a silently wrong bill, on the leaves that cost the most."""
        envelope = json.dumps({
            "result": json.dumps(_valid_bundle()), "is_error": False, "session_id": "s",
            "total_cost_usd": 0.41, "usage": {"input_tokens": 2, "output_tokens": 4},
            "modelUsage": {"claude-opus-5[1m]": {"inputTokens": 2, "outputTokens": 4},
                           "claude-haiku-4-5-20251001": {"inputTokens": None}}})
        c, _refs, oc = self._run([envelope])
        self.assertEqual(oc.status, "pass")
        row = [cap["--agent-run-json"] for sub, cap in c.calls
               if sub == "finalize-child" and "--agent-run-json" in cap][-1]
        self.assertEqual(row["usage"]["total_tokens"], 6)
        self.assertNotIn("cost_usd", row["usage"])

    def test_a_pure_envelope_with_no_usage_is_unavailable_not_unmeasured(self) -> None:
        """The only detector for CLI envelope-shape drift: a well-formed envelope that stopped
        carrying usage. `unavailable` says a channel existed and failed — which is a defect to
        investigate — where `not_measured` would say there was never anything to measure and
        silently reinstate issue #47."""
        envelope = json.dumps({"result": json.dumps(_valid_bundle()), "is_error": False,
                               "session_id": "s"})
        c, _refs, oc = self._run([envelope])
        self.assertEqual(oc.status, "pass")
        row = [cap["--agent-run-json"] for sub, cap in c.calls
               if sub == "finalize-child" and "--agent-run-json" in cap][-1]
        self.assertEqual(row["usage"]["status"], "unavailable")
        self.assertIn("carried no usable usage", row["usage"]["reason"])

    def test_finalize_before_write_ordering(self) -> None:
        # The finalize-child call MUST precede the host artifact writes (empty write_roots make
        # an in-window write unauthorized). Pin the ORDERING directly: capture, at the instant
        # finalize_child runs, whether the bundle artifacts already exist on disk — they must
        # NOT (the writes come strictly after). A regression that moved the writes earlier would
        # find them present here and fail.
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        observed: dict[str, bool] = {}

        class _C(_PureFakeConductor):
            def finalize_child(self, child_arid, return_token, reply_text, agent_run_json):  # type: ignore[override]
                base = self.repo_root / refs.source_dir()
                observed["bundle_exists_at_finalize"] = (base / "codegen_bundle.json").exists()
                observed["model_exists_at_finalize"] = (
                    base / "src" / f"{_SPEC_ID}_model.f90").exists()
                return super().finalize_child(child_arid, return_token, reply_text, agent_run_json)

        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
               llm_config=_cfg("claude"), env={})
        c.envelopes = [_envelope(_valid_bundle())]
        oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        self.assertEqual(oc.status, "pass")
        self.assertFalse(observed["bundle_exists_at_finalize"])
        self.assertFalse(observed["model_exists_at_finalize"])
        # ...and they DO exist after the substep returns (the writes ran, just later).
        self.assertTrue((c.repo_root / refs.source_dir() / "codegen_bundle.json").exists())

    def test_bounded_repair_recovers_on_second_turn(self) -> None:
        bad = _valid_bundle()
        del bad["capability_requirements"]  # schema violation -> repair
        c, refs, oc = self._run([_envelope(bad), _envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.attempts, 2)

    def test_per_attempt_records_failure_of_a_superseded_attempt_only(self) -> None:
        # Item 6 observability: the failed (superseded) attempt carries its failure_category and a
        # bounded failure_excerpt; the passing attempt that supersedes it does not.
        bad = _valid_bundle()
        del bad["capability_requirements"]  # schema violation -> repair
        c, refs, oc = self._run([_envelope(bad), _envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        meta = json.loads((c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
        pa = meta["per_attempt"]
        self.assertEqual(len(pa), 2)
        self.assertEqual(pa[0]["failure_category"], "bundle_schema_violation")
        self.assertTrue(pa[0]["failure_excerpt"])
        self.assertLessEqual(len(pa[0]["failure_excerpt"]), 400)
        self.assertNotIn("failure_category", pa[1])
        self.assertNotIn("failure_excerpt", pa[1])

    def test_checks_abi_violation_is_repaired_in_loop(self) -> None:
        # The whole point of the layer: what cost the sw2d P-arm its retry budget (a phase reopen
        # per guess) is now ONE bounded in-conversation repair.
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content(as_function="metric_compute")
        c, refs, oc = self._run([_envelope(bad), _envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.attempts, 2)

    def test_exhausted_checks_abi_repair_routes_to_generate_reuse(self) -> None:
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content(omit="case_run")
        c, refs, oc = self._run([_envelope(bad)])
        self.assertEqual(oc.status, "fail")
        meta = json.loads((c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
        self.assertEqual(meta["failure_category"], "bundle_checks_abi_violation")
        # The terminal category must carry an outer route, or the phase fails closed instead of
        # reopening its own producer.
        self.assertEqual(
            wc.GENERATE_BUNDLE_FAILURE_ROUTING["bundle_checks_abi_violation"],
            ("generate", "reuse"))

    def test_missing_runner_fails_closed_before_any_leaf_spawn(self) -> None:
        # A host artifact the conductor itself renders is not something a generate retry can fix:
        # fail_closed (operator --resume), and no leaf is spawned to burn tokens on it.
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo, stage_runner=False)
        c = _conductor(repo)
        c.envelopes = [_envelope(_valid_bundle())]
        events: list = []
        _real_emit = c.emit
        def _emit(event, **fields):  # type: ignore[no-untyped-def]
            events.append({"event": event, **fields})
            _real_emit(event, **fields)
        c.emit = _emit  # type: ignore[assignment,method-assign]
        oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.leaf_returncode, 1)
        self.assertEqual(oc.infra_error[0], "pure_context_assembly_failed")
        self.assertEqual(getattr(c, "_spawn", 0), 0)
        # The TWIN of the reviewer's pin (issue #142 round 1 closed only that half; round 2
        # measured this one as a surviving mutant — deleting the two emit lines here left 201
        # tests green). The run-log row is where an operator reads WHICH document was missing:
        # the fail_closed reason downstream carries the tag but not the path.
        rows = [e for e in events if e["event"] == "pure_context_assembly_failed"]
        self.assertEqual(len(rows), 1, events)
        self.assertEqual(rows[0]["node_key"], refs.node_key)
        self.assertIn("pure_runner_document_missing", rows[0]["detail"])

    def test_pass_after_repair_tombstones_orphan_attempts(self) -> None:
        # Review fix (HIGH): a repaired pass must tombstone the earlier (finalized, un-vouched)
        # attempt arids, or the completion gate rejects the otherwise-passing run.
        bad = _valid_bundle()
        del bad["capability_requirements"]
        c, refs, oc = self._run([_envelope(bad), _envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        self.assertTrue(any(sub == "add-superseded-runs" for sub, _ in c.calls))

    def test_exhausted_repair_records_bundle_meta_fail(self) -> None:
        bad = _valid_bundle()
        del bad["capability_requirements"]
        # MAX_BUNDLE_REPAIR_TURNS=2 -> 3 attempts total, all bad.
        c, refs, oc = self._run([_envelope(bad)])
        self.assertEqual(oc.status, "fail")
        meta = json.loads((c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
        self.assertEqual(meta["result"], "fail")
        self.assertEqual(meta["failure_category"], "bundle_schema_violation")
        self.assertTrue(meta.get("failure_excerpt"))

    def test_unparseable_reply_categorized(self) -> None:
        c, refs, oc = self._run([_envelope("not json at all", )])
        self.assertEqual(oc.status, "fail")
        meta = json.loads((c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
        self.assertEqual(meta["failure_category"], "pure_response_unparseable")

    def test_host_write_failure_after_finalize_recovers(self) -> None:
        # Codex P2 (finding 2): a host-side write that fails AFTER finalize_child recorded the
        # attempt as a passing terminal row must NOT leave an un-vouched orphan. The substep must
        # instead route fail_closed (non-zero leaf_returncode + a pure_host_write_failed tag) so
        # run_phase's transport branch tombstones the orphan and the operator resumes.
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        finalized: dict[str, bool] = {}

        class _C(_PureFakeConductor):
            def finalize_child(self, child_arid, return_token, reply_text, agent_run_json):  # type: ignore[override]
                finalized["did"] = True
                return super().finalize_child(child_arid, return_token, reply_text, agent_run_json)

            def _write_pure_bundle_artifacts(self, refs, doc, graph):  # type: ignore[override]
                raise OSError(28, "No space left on device")

        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
               llm_config=_cfg("claude"), env={})
        c.envelopes = [_envelope(_valid_bundle())]
        oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        self.assertTrue(finalized.get("did"))            # the window WAS closed (finalize ran)
        self.assertEqual(oc.status, "fail")
        self.assertNotEqual(oc.leaf_returncode, 0)       # forces run_phase's fail_closed branch
        self.assertEqual(oc.infra_error[0], "pure_host_write_failed")
        # No passing bundle_meta was left behind claiming success.
        base = c.repo_root / refs.source_dir()
        if (base / "bundle_meta.json").exists():
            self.assertNotEqual(json.loads((base / "bundle_meta.json").read_text())["result"],
                                "pass")

    def test_unencodable_valid_bundle_is_schema_violation_not_transport(self) -> None:
        # M-D2 (D1 mirror of the verify reviewer): a bundle that passes every content layer but
        # carries a lone surrogate in a files[].content is not UTF-8 persistable. It must be caught
        # BEFORE accept as a schema violation (repairable, routable via the bundle table) — NOT
        # accepted and then mis-routed through the pass branch's host-write/transport recovery.
        bad = _valid_bundle()
        bad["files"][0]["content"] += "\ud800"
        c, refs, oc = self._run([_envelope(bad)])  # persistently unencodable -> exhaustion
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.leaf_returncode, 0)          # routable, NOT a transport fail_closed
        base = c.repo_root / refs.source_dir()
        # Never accepted => no bundle artifacts authored.
        self.assertFalse((base / "codegen_bundle.json").exists())
        meta = json.loads((base / "bundle_meta.json").read_text())
        self.assertEqual(meta["failure_category"], "bundle_schema_violation")

    def test_bundle_meta_write_failure_on_exhaustion_recovers(self) -> None:
        # M-D2 (D2 mirror of the verify reviewer): the exhaustion-path bundle_meta write must be
        # guarded like the pass path — a host-write failure (ENOSPC, or a non-encodable excerpt)
        # recovers as a fail_closed transport outcome, never an uncaught exception escaping
        # run_substep and crashing the conductor.
        class _C(_PureFakeConductor):
            def _write_bundle_meta(self, *a, **k):  # type: ignore[override]
                raise OSError(28, "No space left on device")
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
               llm_config=_cfg("claude"), env={})
        bad = _valid_bundle()
        del bad["capability_requirements"]  # persistently schema-invalid -> exhaustion
        c.envelopes = [_envelope(bad)]
        oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        self.assertEqual(oc.status, "fail")
        self.assertNotEqual(oc.leaf_returncode, 0)
        self.assertEqual(oc.infra_error[0], "pure_host_write_failed")


# ======================================================================================
# --wait-usage-reset in the pure producer loop
# ======================================================================================
class PureUsageLimitWaitTest(unittest.TestCase):
    """--wait-usage-reset (opt-in) in the pure producer: a transport death (rc!=0) that carries a
    machine-form usage-limit reset epoch is waited out IN PLACE and the SAME turn re-launched,
    instead of falling to the terminal fail branch for a next-day --resume. The wait is NOT a repair
    turn — it must not consume the bundle-repair budget and must not pollute the repair carriers
    (last_excerpt / resume_session_id). Default OFF preserves the current terminal behavior."""

    class _C(_PureFakeConductor):
        def spawn_leaf(self, prompt_text, child_env, entry=None, **kwargs):  # type: ignore[override]
            self._spawn = getattr(self, "_spawn", 0)
            proc = self.procs[min(self._spawn, len(self.procs) - 1)]
            self._spawn += 1
            return proc

        def _sleep_backoff(self, seconds):  # type: ignore[override]
            self.slept.append(seconds)

    def _conductor(self, repo: Path, procs: list, **kw) -> "_C":
        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = self._C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
                    llm_config=_cfg("claude"), env={}, **kw)
        c.procs = procs
        c.slept = []
        return c

    def test_a_pure_leaf_that_wrote_no_envelope_names_the_exit_it_died_at(self) -> None:
        """The other `unavailable` half (issue #47). A leaf killed at the per-leaf cap writes
        nothing to stdout: its tokens were spent and are unrecorded. Separable from a live leaf
        whose envelope merely carried no usage — that one says so — by naming the parse failure
        and the exit code, which is what tells an operator which defect to chase."""
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        c = self._conductor(repo, [wc.ProcResult(-9, "", "boom", timed_out=True)])
        oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        self.assertEqual(oc.status, "fail")
        row = [cap["--agent-run-json"] for sub, cap in c.calls
               if sub == "finalize-child" and "--agent-run-json" in cap][-1]
        self.assertEqual(row["usage"]["status"], "unavailable")
        self.assertIn("no result envelope on leaf stdout", row["usage"]["reason"])
        self.assertIn("leaf_exit=-9", row["usage"]["reason"])

    def test_a_non_claude_pure_leaf_never_unwraps_an_envelope(self) -> None:
        """WIRING pin for the PURE sites' `allow_envelope`. Only a `claude_cli` pure leaf's
        stdout is CLI-authored; a codex or HTTP pure leaf writes the MODEL's answer there, so
        its `is_error` / `api_error_status` keys are forgeable. Unwrapping them would let a
        leaf that crashed for an unrelated reason park the run for hours. The same bytes DO
        arm under a claude entry — asserted here — so only the provider separates them."""
        now = 1_752_200_000.0
        abort = json.dumps({
            "type": "result", "is_error": True, "api_error_status": 429, "num_turns": 1,
            "terminal_reason": "api_error",
            "result": f"Claude AI usage limit reached|{int(now) + 300}"})
        self.assertIsNotNone(wc._sole_content_usage_limit_line(abort, allow_envelope=True))
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        c = self._conductor(repo, [wc.ProcResult(1, abort, "")], wait_usage_reset=True)
        c.llm_config = _cfg("codex", agent_model="gpt-5.6-sol")
        events: list = []
        c.emit = lambda event, **f: events.append((event, f))  # type: ignore[assignment]
        with mock.patch.object(wc.time, "time", return_value=now):
            oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        self.assertEqual(oc.status, "fail")
        self.assertEqual(c.slept, [])                                  # no wait
        self.assertEqual([e for e, _ in events if e == "leaf_usage_limit_wait"], [])
        self.assertEqual([f["reason"] for e, f in events
                          if e == "leaf_usage_limit_wait_declined"], ["no_reset_time"])

    def test_a_timed_out_producer_is_a_leaf_timeout_and_never_waits_out_a_quota(self) -> None:
        """`generate.generate` is the substep of the 99-minute field hang, so this loop's own
        classification of the dead leaf is load-bearing. A leaf the conductor KILLED is tagged
        from `ProcResult.timed_out`; classifying its captured TEXT instead would read the
        throttle notice a wedged leaf had printed as `llm_usage_limit`, and — with the opt-in
        wait ON, as here — park the run for hours before re-launching a leaf the conductor had
        killed itself."""
        marker = wc._leaf_timeout_marker(7200, 7203.0)
        now = 1_752_200_000.0
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = self._conductor(
                repo,
                [wc.ProcResult(-9, "", f"Claude AI usage limit reached|{int(now) + 300}\n"
                                       + marker, timed_out=True)],
                wait_usage_reset=True)
            spawn_kwargs: list = []
            inner = c.spawn_leaf

            def _spawn(prompt_text, child_env, entry=None, **kwargs):  # type: ignore[no-untyped-def]
                spawn_kwargs.append(kwargs)
                return inner(prompt_text, child_env, **kwargs)

            c.spawn_leaf = _spawn  # type: ignore[assignment]
            with mock.patch.object(wc.time, "time", return_value=now):
                oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
            self.assertEqual(oc.status, "fail")
            self.assertEqual(oc.leaf_returncode, -9)
            self.assertEqual(oc.infra_error[0], "leaf_timeout")
            self.assertEqual(c._spawn, 1)          # no re-launch...
            self.assertEqual(c.slept, [])          # ...and no multi-hour park
            self.assertEqual(getattr(c, "usage_probe_calls", 0), 0)
            meta = json.loads(
                (c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
            self.assertEqual(meta["per_attempt"][0]["failure_category"], "pure_transport")
            # The event has to NAME the wedged leaf: `generate.generate` is the substep of the
            # incident this cap exists for, and only the caller knows the node/step/substep.
            self.assertEqual(spawn_kwargs[0]["timeout_context"],
                             {"node_key": refs.node_key, "step": "generate",
                              "substep": "generate",
                              "agent_run_id": spawn_kwargs[0]["child_arid"]})

    def test_transport_usage_limit_waits_then_passes(self) -> None:
        now = 1_752_200_000.0
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = self._conductor(
                repo,
                [wc.ProcResult(1, "", f"Claude AI usage limit reached|{int(now) + 300}"),
                 wc.ProcResult(0, _envelope(_valid_bundle()), "")],
                wait_usage_reset=True)
            with mock.patch.object(wc.time, "time", return_value=now):
                oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
            self.assertEqual(oc.status, "pass")
            self.assertEqual(c._spawn, 2)          # dead attempt + the recovered launch
            # `attempts` is the LAUNCH count (== len(per_attempt)); the repair BUDGET is untouched
            # (a wait is not a repair turn), but the wait's launch is still counted honestly.
            self.assertEqual(oc.attempts, 2)
            self.assertEqual(c.slept, [420.0])     # 300s to the reset + 120s margin
            base = c.repo_root / refs.source_dir()
            self.assertTrue((base / "codegen_bundle.json").exists())
            meta = json.loads((base / "bundle_meta.json").read_text())
            self.assertEqual(meta["result"], "pass")
            self.assertEqual(meta["attempts"], 2)          # launch count == len(per_attempt)
            # both launches are visible as per_attempt rows; the dead one is labeled pure_transport
            self.assertEqual(len(meta["per_attempt"]), 2)
            self.assertEqual(meta["per_attempt"][0]["failure_category"], "pure_transport")
            # the dead usage attempt is tombstoned under the wait's own prefix
            reasons = [cap["--reason"] for s, cap in c.calls if s == "add-superseded-runs"]
            self.assertTrue(any("leaf_usage_limit_wait_orphan" in r for r in reasons))
            # the wait consulted the host `/usage` probe first and fell back to the scrape;
            # the stub is what keeps the suite from spawning the real backend
            self.assertEqual(c.usage_probe_calls, 1)

    def test_transport_usage_limit_waits_on_the_real_cli_abort_envelope(self) -> None:
        """REGRESSION, the production shape THIS loop actually met: the only recorded usage limit to
        strike a pure leaf (`orch_20260719T021249Z_419ebdf9`, `generate.generate`, its run log
        showing `pure_bundle_attempt_failed / pure_transport`) did NOT have its abort pre-empt the
        `--output-format json` envelope — the CLI completed the envelope and put the message in
        `result`, with `is_error` / `api_error_status` / `terminal_reason` stamped alongside. Stdout
        was 773 bytes, so a bare-line-only carve-out declines it and this loop stays inert exactly
        where the expensive substeps live. Bytes below are verbatim from that log (`workspace*/` is
        gitignored, so they are pinned here); the sibling test above uses the machine-form-on-stderr
        shape, which production has never produced."""
        # 2026-07-19 02:12 UTC = 11:12 JST, before the recorded 12:30pm JST reset.
        now = datetime(2026, 7, 19, 11, 12, tzinfo=ZoneInfo("Asia/Tokyo")).timestamp()
        # VERBATIM from that log (771 chars / 773 bytes), including the CLI accounting blocks
        # that dominate envelope size — a re-serialized minimal fixture would be ~250 chars
        # and could not exercise the envelope-size guard at all.
        recorded_stdout = (
            '{"type":"result","subtype":"success","is_error":true,"api_error_status":429,"duration_ms":64'
            '6,"duration_api_ms":0,"num_turns":1,"result":"You\'ve hit your session limit · resets 12:30pm'
            ' (Asia/Tokyo)","stop_reason":"stop_sequence","session_id":"160cb9c9-b595-4a87-aa72-fc1cf86c4'
            '878","total_cost_usd":0,"usage":{"input_tokens":0,"cache_creation_input_tokens":0,"cache_rea'
            'd_input_tokens":0,"output_tokens":0,"server_tool_use":{"web_search_requests":0,"web_fetch_re'
            'quests":0},"service_tier":"standard","cache_creation":{"ephemeral_1h_input_tokens":0,"epheme'
            'ral_5m_input_tokens":0},"inference_geo":"","iterations":[],"speed":"standard"},"modelUsage":'
            '{},"permission_denials":[],"terminal_reason":"api_error","fast_mode_state":"off","uuid":"ab0'
            '9298a-847a-415f-87d6-88205cc51fb4"}')
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = self._conductor(
                repo,
                [wc.ProcResult(1, recorded_stdout, ""),
                 wc.ProcResult(0, _envelope(_valid_bundle()), "")],
                wait_usage_reset=True)
            with mock.patch.object(wc.time, "time", return_value=now):
                oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
            self.assertEqual(oc.status, "pass")
            self.assertEqual(c._spawn, 2)          # waited, then relaunched
            reset = datetime(2026, 7, 19, 12, 30, tzinfo=ZoneInfo("Asia/Tokyo")).timestamp()
            self.assertEqual(c.slept, [reset - now + wc.USAGE_LIMIT_WAIT_MARGIN_SECONDS])

    def test_a_declined_wait_names_the_dead_leaf_and_its_evidence(self) -> None:
        """A decline is the operator's only breadcrumb when a wording is not resolvable, and a bare
        `no_reset_time` is what made the stderr-only bug take two investigations. The event must name
        the dead attempt's arid and quote the classifier's own line, from EVERY loop."""
        now = 1_752_200_000.0
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = self._conductor(
                repo,
                # A usage limit with no resolvable reset -> declines.
                [wc.ProcResult(1, "You've hit your session limit; resets soon", ""),
                 wc.ProcResult(0, _envelope(_valid_bundle()), "")],
                wait_usage_reset=True)
            events: list = []
            c.emit = lambda event, **f: events.append((event, f))  # type: ignore[assignment]
            with mock.patch.object(wc.time, "time", return_value=now):
                c._run_pure_generate_substep(refs, "generate", "generate", None, ())
            declined = [f for e, f in events if e == "leaf_usage_limit_wait_declined"]
            self.assertEqual([f["reason"] for f in declined], ["no_reset_time"])
            self.assertTrue(declined[0]["dead_agent_run_id"])
            self.assertIn("session limit", declined[0]["evidence"])

    def test_transport_wait_does_not_pollute_the_repair_turn(self) -> None:
        """A transport wait must leave the repair carriers clean: the launch AFTER the wait is a
        fresh COLD attempt (no prior_document, no repair_findings carrying the transport summary),
        and the following content violation repairs normally. Sequence: transport(usage) -> wait ->
        cold launch that content-fails -> warm repair -> pass."""
        now = 1_752_200_000.0
        bad = _valid_bundle()
        del bad["capability_requirements"]     # a content (schema) violation
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = self._conductor(
                repo,
                [wc.ProcResult(1, "", f"usage limit reached|{int(now) + 100}"),
                 wc.ProcResult(0, _envelope(bad), ""),
                 wc.ProcResult(0, _envelope(_valid_bundle()), "")],
                wait_usage_reset=True)
            captured: list[dict] = []
            orig = c.record_launch

            def _rec(child_arid, request, entry=None, **kw):  # capture the per-launch request shape
                captured.append(request)
                return orig(child_arid, request)

            c.record_launch = _rec  # type: ignore[assignment]
            with mock.patch.object(wc.time, "time", return_value=now):
                oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
            self.assertEqual(oc.status, "pass")
            self.assertEqual(c._spawn, 3)
            # 3 launches: transport(waited) + cold content-fail + warm repair pass. `attempts` is the
            # launch count; the repair budget saw only 1 turn (the wait did not consume it).
            self.assertEqual(oc.attempts, 3)
            self.assertEqual(c.slept, [220.0])     # 100s + 120s margin
            # the post-wait launch (index 1) is a COLD retry: no prior_document carried from the
            # transport death.
            self.assertNotIn("prior_document", captured[1])
            # the repair turn (index 2) carries the CONTENT failure's findings, never the transport
            # summary ("Connection closed"/"usage limit").
            repair_req = captured[2]
            findings = str(repair_req.get("repair", {}).get("repair_findings", ""))
            self.assertNotIn("usage limit", findings.lower())

    def test_transport_usage_limit_is_terminal_when_flag_off(self) -> None:
        now = 1_752_200_000.0
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = self._conductor(   # wait_usage_reset defaults OFF
                repo,
                [wc.ProcResult(1, "", f"usage limit reached|{int(now) + 300}"),
                 wc.ProcResult(0, _envelope(_valid_bundle()), "")])
            with mock.patch.object(wc.time, "time", return_value=now):
                oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
            self.assertEqual(oc.status, "fail")
            self.assertEqual(oc.leaf_returncode, 1)   # run_phase's transport fail_closed branch
            self.assertEqual(c._spawn, 1)             # no second launch
            self.assertEqual(c.slept, [])
            # Regression pin (byte-identity): the terminal bundle_meta must describe the transport
            # DEATH that terminated the substep — the bookkeeping guard that protects the repair
            # carriers must NOT leak an empty/stale failure_excerpt into the meta.
            meta = json.loads(
                (c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
            self.assertEqual(meta["result"], "fail")
            self.assertEqual(meta["failure_category"], "pure_transport")
            self.assertIn("usage limit", (meta.get("failure_excerpt") or "").lower())
            self.assertEqual(meta["attempts"], 1)     # one launch, one per_attempt row

    def test_transport_terminal_after_a_content_repair_records_the_transport_excerpt(self) -> None:
        # Finding-1 twin: attempt0 content-fails (repair carrier = schema text), attempt1 dies of a
        # transport usage limit with the flag OFF -> terminal. The meta must pair
        # failure_category=pure_transport with the TRANSPORT excerpt, never the stale schema carrier.
        bad = _valid_bundle()
        del bad["capability_requirements"]
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = self._conductor(   # flag OFF
                repo,
                [wc.ProcResult(0, _envelope(bad), ""),
                 wc.ProcResult(1, "", "Claude AI usage limit reached")])
            oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
            self.assertEqual(oc.status, "fail")
            self.assertEqual(oc.leaf_returncode, 1)
            meta = json.loads(
                (c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
            self.assertEqual(meta["failure_category"], "pure_transport")
            excerpt = (meta.get("failure_excerpt") or "").lower()
            self.assertIn("usage limit", excerpt)
            self.assertNotIn("capability_requirements", excerpt)  # not the stale schema carrier
            self.assertEqual(meta["attempts"], 2)     # two launches


class PureTransientWallClockBudgetScopeTest(unittest.TestCase):
    """WHICH attempts the transient wall-clock budget is charged for, driven through the real
    pure producer loop — because the whole rule is where the accumulator sits in that loop.

    The budget exists to stop three doomed re-launches of a request that dies at a fixed
    wall-clock. It must therefore be spent by TRANSIENT deaths and by nothing else: a content
    repair turn produced a document and is not what the budget is about, and a usage-limit wait
    can park for hours. Accumulating above those branches made one slow repair turn refuse the
    very next cheap flake — the opposite of `_pure_transient_retry`'s own promise that the three
    budgets cannot compound."""

    class _C(PureUsageLimitWaitTest._C):
        seconds_per_attempt: "list[float]" = []

        def spawn_leaf(self, prompt_text, child_env, entry=None, **kwargs):  # type: ignore[override]
            index = getattr(self, "_spawn", 0)
            self.clock[0] += self.seconds_per_attempt[
                min(index, len(self.seconds_per_attempt) - 1)]
            return super().spawn_leaf(prompt_text, child_env, entry, **kwargs)

    def _run(self, repo: Path, procs: list, seconds: "list[float]", **kw):
        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = self._C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
                    llm_config=_cfg("claude"), env={}, **kw)
        c.procs, c.slept, c.seconds_per_attempt = procs, [], seconds
        c.clock = [1_752_200_000.0]
        events: list = []
        c.emit = lambda event, **f: events.append({"event": event, **f})  # type: ignore
        refs = _write_node(repo)
        # The two clocks are driven APART on purpose: `time.time` is frozen at the base while
        # `time.monotonic` advances with each launch. The budget measures a DURATION, so it must
        # read the moving one — a fixture that advanced both together could not tell them apart,
        # and that is how the wall-clock reading survived undetected in these loops.
        base = c.clock[0]
        with mock.patch.object(wc.time, "time", lambda: base), \
                mock.patch.object(wc.time, "monotonic", lambda: c.clock[0]):
            oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        return c, oc, events

    _FLAKE = wc.ProcResult(1, "", "API Error: Connection closed mid-response.")

    def test_a_slow_content_repair_turn_does_not_spend_the_transient_budget(self) -> None:
        """The reported defect. `generate.generate` legitimately runs ten minutes on this
        workload, so a bundle that fails validation after 700 s is the ORDINARY case, not a
        pathological one. The two-second network flake that follows must still be retried."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            c, oc, events = self._run(
                repo,
                # a slow, repairable content failure, then a cheap flake, then success
                [wc.ProcResult(0, _envelope('{"bundle_schema_version": "9.9.9"}'), ""),
                 self._FLAKE,
                 wc.ProcResult(0, _envelope(_valid_bundle()), "")],
                seconds=[700.0, 2.0, 5.0])
            self.assertEqual(oc.status, "pass")
            self.assertEqual(c._spawn, 3)
            self.assertEqual([e["event"] for e in events].count("leaf_transient_retry"), 1)
            self.assertEqual(
                [e["event"] for e in events].count("leaf_transient_retry_declined"), 0)

    def test_a_usage_limit_wait_does_not_spend_the_transient_budget(self) -> None:
        """`--wait-usage-reset` parks for as long as the quota window says. Billing that to the
        transient budget would refuse the next flake on time no transient attempt ever spent."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            now = 1_752_200_000.0
            c, oc, events = self._run(
                repo,
                [wc.ProcResult(1, "", f"Claude AI usage limit reached|{int(now) + 300}"),
                 self._FLAKE,
                 wc.ProcResult(0, _envelope(_valid_bundle()), "")],
                seconds=[1200.0, 2.0, 5.0], wait_usage_reset=True)
            self.assertEqual(oc.status, "pass")
            self.assertEqual([e["event"] for e in events].count("leaf_transient_retry"), 1)
            self.assertEqual(
                [e["event"] for e in events].count("leaf_transient_retry_declined"), 0)

    def test_the_budget_accumulates_across_transient_attempts(self) -> None:
        """WHAT the accumulator accumulates, as distinct from where it sits — the loop's copy of
        the arithmetic, which the placement tests above do not touch. Two 350 s transient deaths:
        neither crosses the 600 s budget alone, together they do, so the first retry is granted
        and the second refused. Both wrong wirings are silent without this: never accumulating
        lets a substep spend 1050 s where the rule says 700, and double-counting the attempt that
        just died refuses a 350 s flake that is well inside budget."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            c, oc, events = self._run(
                repo, [self._FLAKE, self._FLAKE, wc.ProcResult(0, _envelope(_valid_bundle()), "")],
                seconds=[350.0, 350.0, 5.0])
            self.assertEqual(oc.status, "fail")
            self.assertEqual(c._spawn, 2)          # granted once, then refused at 700 > 600
            self.assertEqual([e["event"] for e in events].count("leaf_transient_retry"), 1)
            declined = [e for e in events if e["event"] == "leaf_transient_retry_declined"]
            self.assertEqual(len(declined), 1)
            self.assertAlmostEqual(declined[0]["spent_seconds"], 350.0)
            self.assertAlmostEqual(declined[0]["elapsed_seconds"], 350.0)

    def test_a_transient_attempt_that_burned_the_clock_still_refuses(self) -> None:
        """The control: the budget must still do its job for the deaths it IS about."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            c, oc, events = self._run(
                repo, [self._FLAKE, wc.ProcResult(0, _envelope(_valid_bundle()), "")],
                seconds=[613.0])
            self.assertEqual(oc.status, "fail")
            self.assertEqual(c._spawn, 1)
            declined = [e for e in events if e["event"] == "leaf_transient_retry_declined"]
            self.assertEqual(len(declined), 1)
            self.assertEqual(declined[0]["reason"], "wall_clock_budget")


# ======================================================================================
# M-D2: producer cold-fallback / capture-time surrogate safety (verify-side mirror)
# ======================================================================================
class PureProducerColdFallbackSurrogateTests(unittest.TestCase):
    def test_cold_fallback_repair_with_surrogate_does_not_crash(self) -> None:
        # M-D2 (G1 mirror): a bundle carrying an unpaired surrogate goes to the
        # bundle_schema_violation repair path; on a COLD fallback (session not resumable) its
        # prior_document is echoed into the repair prompt that record_launch writes as UTF-8. It
        # must be normalized so the write does not raise UnicodeEncodeError mid-repair.
        class _C(_PureFakeConductor):
            def _claude_session_resumable(self, arid, **kw):  # type: ignore[override]
                return False  # force the cold-fallback repair branch on every turn
            def record_launch(self, child_arid, request, entry=None, **kwargs):  # type: ignore[override]
                # Emulate the real record_launch's UTF-8 prompt persistence to surface any
                # non-encodable prior_document as the real path would.
                json.dumps(request, ensure_ascii=False).encode("utf-8")
                return {"launch_prompt_text": "PROMPT"}
        bad = _valid_bundle()
        bad["files"][0]["content"] += "\ud800"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
            c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
                   llm_config=_cfg("claude"), env={})
            c.envelopes = [_envelope(bad)]
            oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())  # must not raise
            self.assertEqual(oc.status, "fail")

    def test_surrogate_in_findings_does_not_crash_repair_or_meta(self) -> None:
        # M-D2 (G3 mirror): `last_excerpt` (findings) flows into both the repair turn's
        # repair_findings AND bundle_meta's failure_excerpt, both persisted as UTF-8. Capture-time
        # normalization must keep every downstream write from raising even if a violation message
        # ever carried a lone surrogate. Force it by making the validator emit one.
        class _C(_PureFakeConductor):
            def _pure_bundle_violations(self, refs, doc):  # type: ignore[override]
                return ("bundle_schema_violation", "bad field value \ud800 here")
            def record_launch(self, child_arid, request, entry=None, **kwargs):  # type: ignore[override]
                json.dumps(request, ensure_ascii=False).encode("utf-8")  # emulate prompt persist
                return {"launch_prompt_text": "PROMPT"}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
            c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
                   llm_config=_cfg("claude"), env={})
            c.envelopes = [_envelope(_valid_bundle())]  # valid doc; validator forces the violation
            oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())  # must not raise
            self.assertEqual(oc.status, "fail")
            # bundle_meta persisted cleanly (excerpt normalized), and its write did not fail_close.
            meta = json.loads((c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
            self.assertEqual(meta["failure_category"], "bundle_schema_violation")
            self.assertEqual(oc.leaf_returncode, 0)   # NOT a host-write fail_close


# ======================================================================================
# build_launch_request pure variant
# ======================================================================================
class PureLaunchRequestTests(unittest.TestCase):
    def _refs(self, repo):
        return _write_node(repo)

    def test_pure_producer_request_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            refs = self._refs(Path(tmp))
            req = wc.build_launch_request(
                refs, step="generate", substep="generate", orchestration_id="o",
                orchestration_agent_run_id="orch", child_agent_run_id="c",
                agent_model="opus", workflow_mode="dev",
                makefile_host_authored=True, runner_host_authored=True,
                pure_leaf=True, pure_context={"harness_capabilities": "x", "target_profile": "y",
                                              "ir_document": "z", "tests_document": "t",
                                              "runner_document": "program r\nend program\n"})
            self.assertEqual(req["leaf_mode"], "pure")
            self.assertEqual(req["prompt_contract_version"], PURE_PROMPT_CONTRACT_VERSION)
            self.assertEqual(req["allowed_output_paths"], [])
            self.assertEqual(req["skill_name"], "")
            self.assertEqual(req["skill_ref"], "")
            self.assertEqual(req["skill_must_read_refs"], "")
            self.assertIn("pure_context", req)


# ======================================================================================
# Terminal-payload carve-out (M-C precondition 修正2)
# ======================================================================================
class PureTerminalCarveOutTests(unittest.TestCase):
    def _setup(self, tmp):
        repo = Path(tmp)
        arid = "child-pure-1"
        launches = repo / "workspace" / "orchestrations" / "o" / "launches"
        launches.mkdir(parents=True, exist_ok=True)
        (launches / f"{arid}.request.json").write_text(
            json.dumps({"leaf_mode": "pure", "step": "generate", "substep": "generate"}),
            encoding="utf-8")
        return repo, arid

    def test_pure_pass_row_requires_empty_output_refs(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, arid = self._setup(tmp)
            with patch.object(ort, "_validate_actual_write_paths", return_value=None):
                # empty output_refs accepted
                ort._validate_terminal_run_payload(
                    repo, "o", {"agent_role": "substep", "status": "pass",
                                "agent_run_id": arid, "output_refs": []})
                # non-empty rejected (forged provenance)
                with self.assertRaises(ValueError):
                    ort._validate_terminal_run_payload(
                        repo, "o", {"agent_role": "substep", "status": "pass",
                                    "agent_run_id": arid, "output_refs": ["x/y.f90"]})
                # Codex P2 (finding 4): a MISSING field is not "empty" — a tampered pass row that
                # drops output_refs must be rejected, not read as [] and waved through.
                with self.assertRaises(ValueError):
                    ort._validate_terminal_run_payload(
                        repo, "o", {"agent_role": "substep", "status": "pass",
                                    "agent_run_id": arid})


# ======================================================================================
# Exemplar attachment on the pure launch (defect B)
# ======================================================================================
class _RenderingFakeConductor(_PureFakeConductor):
    """`_PureFakeConductor` whose record-launch runs the REAL prompt pipeline.

    The base stub returns a literal "PROMPT", so every prompt-content regression is invisible to
    it — exactly how defect B (`exemplar=` never passed to `build_launch_request`) survived a
    green suite. Here record-launch drives the real `prepare_launch_request_payload` +
    `_validate_launch_prompt_text` over the conductor's ACTUAL request, so a request the
    validators would reject fails the test too. Same anti-mock-green shape as the
    finalize-payload test above.
    """

    exemplar_value: dict | None = None

    def _resolve_exemplar(self, refs):  # type: ignore[override]
        # Canned: the real selector needs a certified sibling on disk, which this fixture has no
        # reason to build — the wiring under test is whether the resolved value reaches the render.
        return self.exemplar_value

    def runtime(self, args, *, input=None):  # type: ignore[override]
        out = super().runtime(args, input=input)
        if args[0] != "record-launch":
            return out
        request = self._resolve_evidence(args[args.index("--request-json-file") + 1])
        self.requests = getattr(self, "requests", [])
        self.requests.append(request)
        prepared = ort.prepare_launch_request_payload(dict(request))
        prompt = prepared["launch_prompt_full"]
        ort._validate_launch_prompt_text(prepared, prompt)
        return {"launch_prompt_text": prompt}

    def spawn_leaf(self, prompt_text, child_env, entry=None, **kwargs):  # type: ignore[override]
        self.prompts = getattr(self, "prompts", [])
        self.prompts.append(prompt_text)
        return super().spawn_leaf(prompt_text, child_env, **kwargs)


class PureProducerExemplarTests(unittest.TestCase):
    """R5 exemplar injection on the pure `generate.generate` launch.

    REGRESSION (billed E2E, 2026-07-16 — defect B): every other strand of the pure exemplar
    wiring was in place (the template's `<exemplar>` slot, `_build_exemplar`, the
    `build_launch_request` attach condition, the scan carve-out) but `_run_pure_generate_substep`
    never passed `exemplar=`, so the pure producer authored with strictly less information than
    the legacy leaf it was being A/B'd against.
    """

    _EXEMPLAR = {
        "node_key": "component/sibling@1.0.0",
        "sources": [{"filename": "sibling_model.f90",
                     "text": "module sibling_model\n  ! prior art body\nend module sibling_model\n"}],
    }

    def _run(self, envelopes, repair=None):
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = _RenderingFakeConductor(
            repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
            llm_config=_cfg("claude"), env={})
        c.exemplar_value = self._EXEMPLAR
        c.envelopes = envelopes
        oc = c._run_pure_generate_substep(refs, "generate", "generate", repair, ())
        return c, oc

    def tearDown(self) -> None:
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_cold_pure_launch_renders_exemplar_in_prompt(self) -> None:
        c, oc = self._run([_envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(c.requests[0]["exemplar"], self._EXEMPLAR)
        # ...and it survives the real render + the real launch validator (no fail-close).
        self.assertIn("Certified exemplar (conductor-injected PRIOR ART", c.prompts[0])
        self.assertIn("! prior art body", c.prompts[0])

    def test_warm_repair_attempt_does_not_attach_exemplar(self) -> None:
        # The repair template has no `<exemplar>` slot, so attaching it to a repair turn would
        # ship payload bytes nothing renders.
        bad = _valid_bundle()
        del bad["capability_requirements"]
        c, oc = self._run([_envelope(bad), _envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.attempts, 2)
        self.assertNotIn("exemplar", c.requests[1])
        self.assertNotIn("Certified exemplar", c.prompts[1])

    def test_outer_reopen_without_findings_renders_launch_prompt_with_exemplar(self) -> None:
        """The ONE case where the attach predicate differs from the legacy `not warm_resume`.

        An outer reopen seeded with NO findings excerpt is warm (a session to resume) yet still
        renders the full LAUNCH template — so it wants the exemplar, and the legacy condition
        would have wrongly withheld it. This drives the conductor's predicate and the renderer's
        dispatch against each other through the real renderer on the exact case where they could
        disagree; every other test covers a case where the two happen to agree.
        """
        c, oc = self._run(
            [_envelope(_valid_bundle())],
            repair={"repair_strategy": "reuse", "repair_target_agent_run_id": "prior-arid"})
        self.assertEqual(oc.status, "pass")
        # The launch template rendered (not the repair template) ...
        self.assertIn("Authoring rules", c.prompts[0])
        # ... and the exemplar rode along with it.
        self.assertEqual(c.requests[0]["exemplar"], self._EXEMPLAR)
        self.assertIn("Certified exemplar (conductor-injected PRIOR ART", c.prompts[0])


# ======================================================================================
# Cold-repair prompt contract (M-C 修正4)
# ======================================================================================
class PureColdRepairPromptTests(unittest.TestCase):
    @staticmethod
    def _generate_template_variants() -> "list[tuple[str, str]]":
        """Every `(substep, pure_shape)` a pure GENERATE launch can render.

        WHAT IS DERIVED AND WHAT IS NOT, because a round-3 census caught the first version of
        this docstring overclaiming: the SHAPED variants are read off
        `PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE`, which is the table the launch validator refuses a
        `pure_shape` against — so a new shape genuinely cannot be omitted. The DEFAULT-shape
        pair is derived from `_PROMPT_TEMPLATE_FILES` instead, because no by-shape table
        mentions it, and deriving it is what closes the remaining hole: a new default-shape
        GENERATE substep template would otherwise be silently unexercised, which is the exact
        class this helper was introduced to close.

        `pure_shape` is `""` for the default shape, which is how the request spells it."""
        # The escalate diagnostician holds a `pure generate.diagnose` key of its own and is NOT
        # one of these: it has no static rule paragraphs and no repair loop (issue #169 PR-1).
        # Excluded by asking `DIAGNOSE_LAUNCH_PAIRS`, so a second diagnose pair follows.
        diagnose = {substep for step, substep in ort.DIAGNOSE_LAUNCH_PAIRS
                    if step == "generate"}
        default = sorted(
            key.split(".", 1)[1] for key in ort._PROMPT_TEMPLATE_FILES
            if key.startswith("pure generate.") and key.count(".") == 1
            and key.split(".", 1)[1] not in diagnose)
        out = [(substep, "") for substep in default]
        out += [(substep, shape)
                for step, substep, shape in sorted(ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE)
                if step == "generate"]
        return out

    def _req(self, *, shape: str = "", **overrides):
        substep = overrides.get("substep", "generate")
        if shape:
            context = {k: f"<{k} body>"
                       for k in ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[
                           ("generate", substep, shape)]}
        else:
            context = {"harness_capabilities": "hc", "target_profile": "tp",
                       "ir_document": "ir", "tests_document": "tt",
                       "runner_document": "program r\nend program\n"}
        req = {
            "leaf_mode": "pure", "step": "generate", "substep": "generate",
            "node_key": _NODE, "orchestration_id": "o", "agent_run_id": "c",
            "prompt_contract_version": PURE_PROMPT_CONTRACT_VERSION,
            "repair_findings": "capability_requirements missing",
            "pure_context": context,
            "prior_document": '{"bundle_schema_version": "1.0.0"}',
        }
        if shape:
            req["pure_shape"] = shape
            req["node_key"] = _HARNESS
        req.update(overrides)
        return req

    def test_cold_repair_includes_output_contract_and_prior_document(self) -> None:
        text = ort._render_pure_repair_prompt(self._req())
        self.assertIn("Output contract", text)
        self.assertIn("prior document under repair", text)
        self.assertIn("bundle_schema_version", text)  # the prior document body

    def test_warm_repair_omits_output_contract_and_prior_document(self) -> None:
        text = ort._render_pure_repair_prompt(self._req(warm_resume=True))
        self.assertNotIn("Output contract", text)
        self.assertNotIn("prior document under repair", text)

    def test_cold_repair_includes_dependency_facts(self) -> None:
        # Codex P2: a cold-fallback repair must re-inline the host-resolved dependency facts (the
        # initial launch injects them), else a component-dependent node could re-author code
        # without its dependency APIs. `resolved_dependencies` is threaded on every repair turn.
        req = self._req(resolved_dependencies=[
            {"node_key": "component/foo@1.0.0", "pipeline_ref": "workspace/x",
             "run_id": "r1", "aggregate_verdict_ref": "workspace/v"}])
        text = ort._render_pure_repair_prompt(req)
        self.assertIn("Dependency facts", text)
        self.assertIn("component/foo@1.0.0", text)

    def test_warm_repair_omits_dependency_facts(self) -> None:
        # A warm resume already holds the dependency facts from the initial turn — omit them.
        req = self._req(warm_resume=True, resolved_dependencies=[
            {"node_key": "component/foo@1.0.0", "pipeline_ref": "workspace/x",
             "run_id": "r1", "aggregate_verdict_ref": "workspace/v"}])
        text = ort._render_pure_repair_prompt(req)
        self.assertNotIn("Dependency facts", text)

    def test_output_contract_lift_is_the_whole_paragraph(self) -> None:
        # Guards against silent truncation: if a template edit splits the "Output contract"
        # paragraph with a blank line, `split("\n\n")` would drop everything after it and the
        # cold repair would ship a schema-thin contract. Pin both the heading (start) and the
        # paragraph's final clause (end) so either kind of drift fails the suite rather than
        # silently degrading a cold repair turn.
        text = ort._pure_output_contract_text(self._req())
        self.assertTrue(text.startswith("Output contract"))
        self.assertIn("diagnose it", text)  # the closing clause of the generate.generate contract

    def test_cold_repair_includes_authoring_rules_and_output_contract(self) -> None:
        # A cold fallback re-authors the whole bundle with no prior turn, so it needs the
        # deterministic-gate rules for the same reason it needs the schema (M-C cold-repair
        # contract, extended for defect C).
        text = ort._render_pure_repair_prompt(self._req())
        self.assertIn("Output contract", text)
        self.assertIn("Authoring rules", text)
        # Rule (2)'s subject, which inverted with issue #111: the template used to MANDATE
        # `! allow(C003)` and now forbids every allow directive, so the literal that pins the
        # rule's presence is the prohibition rather than the directive.
        self.assertIn("--ignore-allow-comments", text)

    def test_cold_repair_lifts_every_static_rule_paragraph(self) -> None:
        # The ABI paragraph was added to the launch template and silently not lifted, so a cold
        # repair handed the producer the runner source with no statement that it must publish all
        # ten as subroutines — Z2 defect D reproduced in the RECOVERY path, which is reached
        # exactly when recovery is happening. Assert against the template's own paragraphs, not a
        # literal list, so a third static paragraph cannot be silently omitted the same way.
        #
        # The list serves BOTH pure templates (issue #142 added the verify entries), so a prefix
        # is checked against the template that HOLDS it. Two properties, and the second is what
        # keeps the first from going vacuous: every paragraph present in a template is lifted into
        # that template's cold repair, AND every prefix in the list is present in at least one
        # template — a prefix that matches nothing anywhere is a typo silently lifting nothing.
        placed = {prefix: [] for prefix in ort.PURE_REPAIR_STATIC_PARAGRAPH_PREFIXES}
        # Every pure GENERATE template, derived from the renderer's own table rather than a
        # hand-written pair. A round-2 reviewer measured what the pair cost: issue #169 added two
        # `.harness` templates whose rule paragraphs opened with prefixes this list did not
        # carry, and neither this row nor its sibling below drove those keys — so a cold-fallback
        # repair shipped without the shape's file rules (producer) and without a single checklist
        # item (reviewer), with the suite green. Deriving the keys is what makes a THIRD template
        # impossible to omit the same way.
        for substep, shape in self._generate_template_variants():
            template = ort._load_launch_prompt_templates()[
                ort._pure_launch_template_name({"step": "generate", "substep": substep,
                                                "pure_shape": shape})]
            text = ort._render_pure_repair_prompt(self._req(substep=substep, shape=shape))
            for prefix in ort.PURE_REPAIR_STATIC_PARAGRAPH_PREFIXES:
                if prefix not in template:
                    continue
                placed[prefix].append(substep)
                start = template.index(prefix)
                end = template.index("\n\n", start)
                for line in (ln.strip() for ln in template[start:end].splitlines()):
                    if line and not line.startswith("<"):  # the doc slot is filled elsewhere
                        self.assertIn(line, text,
                                      f"cold repair ({substep}, {shape or 'default'}) "
                                      f"dropped: {line[:60]}")
        for prefix, substeps in placed.items():
            self.assertTrue(substeps, f"prefix matches no pure template: {prefix!r}")

    def test_placeholder_drop_keeps_rule_text_that_mentions_placeholders(self) -> None:
        # The drop is `fullmatch` on the STRIPPED line, so only a line that is nothing but a
        # document slot goes. Rule text quoting a `<...>` metavariable — `use <module>, only:
        # <names>`, the `associate (unused_<name> => <name>)` binding — must survive; dropping it
        # would silently delete a rule from every cold repair.
        text = ort._render_pure_repair_prompt(self._req())
        self.assertIn("use <module>, only: <names>", text)
        self.assertIn("associate (unused_<name> => <name>); end associate", text)

    def test_cold_repair_leaks_no_document_placeholder(self) -> None:
        # A lifted paragraph ends with the `<doc>` slot its launch template fills; nothing
        # substitutes it here, so it would ship as a literal token.
        text = ort._render_pure_repair_prompt(self._req())
        for slot in ("<runner_document>", "<ir_document>", "<tests_document>", "<exemplar>"):
            self.assertNotIn(slot, text)

    def test_warm_repair_omits_authoring_rules(self) -> None:
        # The resumed session already holds the launch prompt's static prefix.
        text = ort._render_pure_repair_prompt(self._req(warm_resume=True))
        self.assertNotIn("Authoring rules", text)
        self.assertNotIn("--ignore-allow-comments", text)

    def test_authoring_rules_lift_is_the_whole_paragraph(self) -> None:
        # An interior blank line introduced by a template edit would make `split("\n\n")` drop
        # everything after it, shipping a cold repair with only the first rule groups. Assert
        # STRUCTURALLY (not just heading + current closing clause): everything the template holds
        # between the heading and the next section must survive the lift. A literal-anchored pin
        # catches truncation of today's text but NOT an append — a rule (6) added after a blank
        # line would vanish from the cold repair with the test still green.
        text = ort._pure_authoring_rules_text(self._req())
        self.assertTrue(text.startswith("Authoring rules"))
        template = ort._load_launch_prompt_templates()["pure generate.generate"]
        start = template.index("Authoring rules")
        end = template.index("**Harness capabilities")  # the next section of the static prefix
        for line in (ln.strip() for ln in template[start:end].splitlines()):
            if line:
                self.assertIn(line, text)

    def test_verify_lift_is_the_whole_reviewer_paragraphs_and_nothing_else(self) -> None:
        # Until issue #142 this asserted the verify lift was EMPTY. It is deliberately not, now:
        # the reviewer's cold repair carries the `Review checklist` (G1-G7 plus the
        # deterministic-gate exclusion) and the inlined contract's label + scope bound, because
        # `pure_context` re-inlines the 9 KB ABI document into that prompt regardless.
        #
        # TWO properties, and the second is the one a round-2 reviewer had to supply. (a) Nothing
        # foreign is lifted: every block starts with a prefix that occurs in THIS template — which
        # is the real statement of "no producer rule leaks into a reviewer turn", where asserting
        # the absence of three strings that do not occur in the verify template at all pinned
        # nothing. (b) Nothing is TRUNCATED: the lift is a `\n\n` split, so a blank line inserted
        # into either paragraph silently drops everything after it — measured, inserting one
        # before `G5 — io_contract` dropped G5/G6/G7 from every cold verify repair and left 197
        # tests green, because the previous test computed its expectation with `template.index(
        # "\n\n", start)`, the very mechanism under test. The terminators below are INDEPENDENT
        # of that split, exactly as the producer's `test_authoring_rules_lift_is_the_whole_
        # paragraph` uses `**Harness capabilities`. Issue #143 added the severity-rubric label as
        # a third pair rather than widening the contract pair's terminator: with one span running
        # from the contract label to the bundle, the contract paragraph's own end would stop being
        # pinned the moment a paragraph between them is lifted.
        req = self._req(substep="verify")
        lifted = ort._pure_authoring_rules_text(req)
        template = ort._load_launch_prompt_templates()["pure generate.verify"]

        # (a) every lifted block is a paragraph of THIS template
        for block in lifted.split("\n\n"):
            head = block.lstrip().splitlines()[0]
            self.assertTrue(
                any(head.startswith(pfx) for pfx in ort.PURE_REPAIR_STATIC_PARAGRAPH_PREFIXES),
                f"lifted block is not a declared static paragraph: {head[:70]}")
            self.assertIn(head, template, f"lifted block is foreign to this template: {head[:70]}")

        # (b) each paragraph survives WHOLE, bounded by a terminator the split cannot move
        for prefix, terminator in (("Review checklist", "**Controlled spec"),
                                   ("**Checks-module contract (", "**Severity rubric ("),
                                   ("**Severity rubric (", "**Generated CodegenBundle")):
            start = template.index(prefix)
            end = template.index(terminator)
            self.assertGreater(end, start, (prefix, terminator))
            for line in (ln.strip() for ln in template[start:end].splitlines()):
                if line and not line.startswith("<"):   # the doc slot is filled elsewhere
                    self.assertIn(line, lifted, f"verify lift dropped: {line[:70]}")

        self.assertNotIn("<authoring_rules>", ort._render_pure_repair_prompt(req))

    def test_harness_verify_lift_is_the_whole_reviewer_paragraphs(self) -> None:
        """The same two properties on the `harness` shape's reviewer template (issue #169).

        Its checklist opens with a scope paragraph and an input-side clause that the m3c one
        states elsewhere; those were separate `\n\n` blocks until a round-2 reviewer measured
        the cold repair carrying the checklist HEADER and none of H1-H10 — the reviewer would
        re-judge holding "the deterministic gate already ran, do NOT re-check style" and no
        checklist at all. They are one paragraph now, and the terminators below are INDEPENDENT
        of the `\n\n` split that does the lifting, so re-introducing a blank line is red here."""
        req = self._req(substep="verify", shape="harness")
        lifted = ort._pure_authoring_rules_text(req)
        template = ort._load_launch_prompt_templates()["pure generate.verify.harness"]

        for block in lifted.split("\n\n"):
            head = block.lstrip().splitlines()[0]
            self.assertTrue(
                any(head.startswith(pfx) for pfx in ort.PURE_REPAIR_STATIC_PARAGRAPH_PREFIXES),
                f"lifted block is not a declared static paragraph: {head[:70]}")
            self.assertIn(head, template, f"lifted block is foreign to this template: {head[:70]}")

        for prefix, terminator in (("Review checklist", "**Controlled spec"),
                                   ("**Runner-output contract (", "**Severity rubric ("),
                                   ("**Severity rubric (", "**Generated CodegenBundle")):
            start = template.index(prefix)
            end = template.index(terminator)
            self.assertGreater(end, start, (prefix, terminator))
            for line in (ln.strip() for ln in template[start:end].splitlines()):
                if line and not line.startswith("<"):
                    self.assertIn(line, lifted, f"harness verify lift dropped: {line[:70]}")
        # ...including the last checklist item, which is what a re-introduced blank line eats.
        self.assertIn("H10 — dependency consistency", lifted)

    def test_harness_generate_lift_carries_the_shape_rules(self) -> None:
        """The producer half. `File shape (` is the ONLY statement of this shape's host-enforced
        rules — one model named and declaring `<spec_id>_model`, one runner, no checks file — so
        a cold repair of a `bundle_shape_unsupported` finding without it hands the leaf the
        findings text and not the contract it violated (the recorded Z2 defect D, in the
        recovery path)."""
        lifted = ort._pure_authoring_rules_text(self._req(shape="harness"))
        template = ort._load_launch_prompt_templates()["pure generate.generate.harness"]
        for prefix, terminator in (("What makes this shape different", "Output contract ("),
                                   ("File shape (", "Authoring rules ("),
                                   ("Authoring rules (", "**Harness capabilities"),
                                   ("**Runner-output contract (", "Target node_key:")):
            start = template.index(prefix)
            end = template.index(terminator)
            self.assertGreater(end, start, (prefix, terminator))
            for line in (ln.strip() for ln in template[start:end].splitlines()):
                if line and not line.startswith("<"):
                    self.assertIn(line, lifted, f"harness generate lift dropped: {line[:70]}")
        self.assertIn("EXACTLY ONE file of role `model`", lifted)
        self.assertIn("(11)", lifted)  # the last authoring rule

    def test_cold_repair_paragraphs_are_lifted_in_template_order(self) -> None:
        # The loop iterated `PURE_REPAIR_STATIC_PARAGRAPH_PREFIXES` while its docstring said
        # "in template order". Inert while the generate template's three paragraphs happened to
        # sit in tuple order; issue #142 added two more, and a readability reorder of the tuple
        # would then have silently reordered a cold repair against the launch prompt a warm
        # session holds, with nothing red. Measured before the fix: reordering the tuple put the
        # contract label ahead of the checklist and left 197 tests green.
        for substep in ("generate", "verify"):
            template = ort._load_launch_prompt_templates()[f"pure generate.{substep}"]
            lifted = ort._pure_authoring_rules_text(self._req(substep=substep))
            heads = [b.lstrip().splitlines()[0] for b in lifted.split("\n\n") if b.strip()]
            self.assertEqual(heads, sorted(heads, key=template.index),
                             f"pure generate.{substep}: lift order is not template order")

    def test_style_lint_distills_c072_character_dummy_widths(self) -> None:
        # Fail #1 (orch_9a5fe93e): the pure leaf authored `character(len=*), intent(out)` and
        # burned a retry on fortitude C072. The doc-blind producer learns the rule only if the
        # launch template states it. Under the per-id ABI (pure-8) the sole `intent(out)` character
        # dummy is `status`, whose width MUST match the one authority the runner renders against
        # (the fortran backend runner's CHECK_STATUS_WIDTH), not a hand-copied number that could drift.
        from tools.backends.language.fortran.runner import CHECK_STATUS_WIDTH
        template = ort._load_launch_prompt_templates()["pure generate.generate"]
        start = template.index("(1) Style lint")
        end = template.index("\n(2)", start)
        style = template[start:end]
        self.assertIn("C072", style)
        self.assertIn("checks_compute(case_id, check_id, status)", style)
        self.assertIn(f"character(len={CHECK_STATUS_WIDTH})", style)


# ======================================================================================
# post_generate bundle re-validation
# ======================================================================================
class PurePostGenerateBundleTests(unittest.TestCase):
    def _gen_dir(self, tmp):
        repo = Path(tmp)
        # The tamper gate now re-runs the FULL acceptance contract, so it reads the IR + sidecar
        # for capability negotiation / state vars / assembly graph — write them (returns ir_ref).
        refs = _write_node(repo)
        gen = repo / "src" / _SPEC_ID
        (gen / "src").mkdir(parents=True, exist_ok=True)
        bundle = _valid_bundle()
        (gen / "codegen_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
        for entry in bundle["files"]:
            (gen / "src" / entry["logical_path"]).write_text(entry["content"], encoding="utf-8")
        # The real rendered runner, not a `program p` stub: it is the checks-ABI layer's
        # authority, so a stub would make the gate unverifiable (fail-closed) here.
        (gen / "src" / f"{_SPEC_ID}_runner.f90").write_text(_runner_text(), encoding="utf-8")
        return repo, gen, refs.ir_ref

    def test_clean_bundle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
            self.assertEqual(v, [])

    def test_an_undeclared_source_is_refused_and_the_host_runner_is_the_only_exception(
            self) -> None:
        """The provenance gate's carve-out, pinned in BOTH directions.

        `allowed_extra` decides which undeclared `.f90` this gate tolerates, and it is exactly
        one file: the runner the host renders. A mutation sweep found that widening it left the
        whole validator suite green — so the one function that now owns the runner's name
        (`_expected_runner_name`) was the sole guard of a provenance check with no witness.

        Two rows in one, because a carve-out needs both: the exception is ACCEPTED (the clean
        bundle rows above already fail if it is not), and everything else is REFUSED. A pin on
        the refusal alone would survive widening the set to two names."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            (gen / "src" / "smuggled.f90").write_text(
                "module smuggled\nend module smuggled\n", encoding="utf-8")
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
        self.assertTrue([x for x in v if "smuggled.f90" in x and "undeclared" in x], v)
        # The host-rendered runner is present in the same tree and must NOT be reported: it is
        # the carve-out, and a widened set is caught by the row below rather than here. Filter
        # on the violation's SUBJECT (the path before the first colon), not on the whole line:
        # the message NAMES the glue set it admitted, so a substring test over the line matches
        # every undeclared-source violation and pins nothing (it did, until issue #169 made the
        # set shape-dependent and the row went red for the right reason).
        subjects = [x.split(":", 1)[0] for x in v]
        self.assertEqual([], [x for x in subjects if x.endswith(f"{_SPEC_ID}_runner.f90")], v)
        # The refusal NAMES the glue it admitted, so the operator reading it can tell an
        # undeclared source from a carve-out that did not fire. The name is derived from
        # `_expected_runner_name`, the one function that says what the host renders.
        clause = next(x for x in v if "smuggled.f90" in x)
        self.assertIn(vps._expected_runner_name(_SPEC_ID), clause)

    def test_the_build_graph_is_told_which_source_is_host_glue(self) -> None:
        """`host_glue_sources` is what makes the runner's object name a KNOWN one.

        A census reviewer found it unwitnessed: with the wrong name passed, a bundle declaring a
        file at the runner's own logical path stops earning the `bundle_assembly_collision`
        refusal — the contract-boundary capture — and falls through to a weaker tamper
        violation. The node is still refused, so what is lost is the REASON, which is what an
        operator and a repair leaf act on.

        Driven through the real gate with a bundle that collides, rather than by reading the
        argument."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            spec_id = vps._spec_id_from_node_key(_NODE)
            assert spec_id is not None
            bundle = json.loads((gen / "codegen_bundle.json").read_text())
            # A declared file whose logical path IS the host glue: the assembly graph must see
            # the collision.
            bundle["files"].append({
                "logical_path": vps._expected_runner_name(spec_id),
                "role": "helper", "language": "fortran", "member_node_key": None,
                "content": "module zzz_collide\nend module zzz_collide\n",
                "modules": ["zzz_collide"],
            })
            (gen / "codegen_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
        self.assertTrue([x for x in v if "collision" in x],
                        f"the assembly collision must be the reported reason: {v}")

    def test_the_carve_out_admits_exactly_the_host_rendered_runner_name(self) -> None:
        """The carve-out is a SET OF ONE, derived from the node's `spec_id`.

        Pinned against the function that derives it rather than against the literal, so a rename
        moves both together; and pinned as EQUALITY, because a superset is what the mutation
        that survived actually produced."""
        spec_id = vps._spec_id_from_node_key(_NODE)
        self.assertIsNotNone(spec_id)
        expected = vps._expected_runner_name(spec_id)
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            # A file whose name differs from the carve-out by one character must be refused.
            near_miss = expected.replace("_runner.f90", "_runners.f90")
            (gen / "src" / near_miss).write_text(
                "module x\nend module x\n", encoding="utf-8")
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
        self.assertTrue([x for x in v if near_miss in x and "undeclared" in x], v)
        self.assertEqual([], [x for x in v if f"/{expected}" in x], v)

    def test_absent_bundle_is_inert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            gen = repo / "src" / _SPEC_ID
            gen.mkdir(parents=True)
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, None, v)
            self.assertEqual(v, [])

    def test_tampered_content_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            (gen / "src" / f"{_SPEC_ID}_model.f90").write_text("tampered\n", encoding="utf-8")
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
            self.assertTrue(any("tamper" in s for s in v), v)

    def test_checks_abi_tamper_survives_byte_compare_but_is_caught(self) -> None:
        # A CONSISTENT tamper (bundle content and the staged .f90 edited together) defeats the
        # byte-compare mirror, so only the re-run acceptance contract can catch it. Drops a
        # subroutine the runner calls.
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            bundle = json.loads((gen / "codegen_bundle.json").read_text())
            bundle["files"][1]["content"] = _checks_content(omit="checks_compute")
            (gen / "codegen_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
            (gen / "src" / f"{_SPEC_ID}_checks.f90").write_text(
                bundle["files"][1]["content"], encoding="utf-8")
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
            self.assertTrue(any("bundle_checks_abi_violation" in s for s in v), v)

    def test_undeclared_f90_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            (gen / "src" / "sneaky.f90").write_text("module sneaky\nend module\n", encoding="utf-8")
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
            self.assertTrue(any("undeclared" in s for s in v), v)

    def test_undeclared_uppercase_F90_flagged(self) -> None:
        # Codex P2 (finding 2): an uppercase .F90 suffix must be caught too — a case-sensitive
        # rglob("*.f90") would miss it and let the undeclared source bypass the provenance check.
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            (gen / "src" / "sneaky.F90").write_text("module sneaky\nend module\n", encoding="utf-8")
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
            self.assertTrue(any("undeclared" in s for s in v), v)

    def test_capability_tamper_flagged(self) -> None:
        # Codex P2 (finding 1): a schema-VALID but unsupported capability swap leaves every source
        # byte unchanged; the gate must still reject it via the reused capability negotiation.
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            bundle = _valid_bundle()
            bundle["capability_requirements"] = ["batched_cases@1"]  # schema-valid, unsupported
            (gen / "codegen_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
            self.assertTrue(any("bundle_capability_unsatisfied" in s for s in v), v)


# ======================================================================================
# run_workflow generate-executor surface (M-F: flag + env removed, pure is the only executor)
# ======================================================================================
class GenerateExecutorFlagTests(unittest.TestCase):
    def test_cold_run_has_no_executor_gate(self) -> None:
        # M-F: a cold run has NO executor gate — the executor is hardcoded pure. The run proceeds
        # straight to normal startup resolution (failing here on the bogus spec, not on any
        # executor block), and does NOT emit the retired generate_executor_invalid reason nor stamp
        # the removed ATMOFAB_GENERATE_EXECUTOR env.
        import io
        from contextlib import redirect_stdout
        import tools.run_workflow as rw
        buf = io.StringIO()
        prev = os.environ.pop("ATMOFAB_GENERATE_EXECUTOR", None)
        try:
            with redirect_stdout(buf):
                # `--llm-config` explicitly: this runs against the real checkout, whose
                # `./llm.yaml` is the operator's own and may or may not exist. Without it the
                # run stops at `llm_config_default_missing` on a clean machine and at the
                # bogus spec on a configured one — the same assertion for two different
                # reasons.
                rc = rw.main(["spec/nonexistent_xyz", "generate",
                              "--llm-config", "docs/examples/llm_claude.example.yaml"])
            self.assertEqual(rc, 2)
            self.assertIn("invalid_startup_input", buf.getvalue())
            self.assertNotIn("generate_executor_invalid", buf.getvalue())
            self.assertNotIn("ATMOFAB_GENERATE_EXECUTOR", os.environ)
        finally:
            if prev is not None:
                os.environ["ATMOFAB_GENERATE_EXECUTOR"] = prev

    def test_ambient_env_executor_ignored(self) -> None:
        # M-F: ATMOFAB_GENERATE_EXECUTOR is fully inert. A stale ambient value (even an old typo)
        # is neither read nor validated — no generate_executor_invalid, and the run proceeds to
        # normal startup resolution.
        import io
        from contextlib import redirect_stdout
        import tools.run_workflow as rw
        buf = io.StringIO()
        prev = os.environ.get("ATMOFAB_GENERATE_EXECUTOR")
        os.environ["ATMOFAB_GENERATE_EXECUTOR"] = "pur"
        try:
            with redirect_stdout(buf):
                # `--llm-config` explicitly: this runs against the real checkout, whose
                # `./llm.yaml` is the operator's own and may or may not exist. Without it the
                # run stops at `llm_config_default_missing` on a clean machine and at the
                # bogus spec on a configured one — the same assertion for two different
                # reasons.
                rc = rw.main(["spec/nonexistent_xyz", "generate",
                              "--llm-config", "docs/examples/llm_claude.example.yaml"])
            self.assertEqual(rc, 2)
            self.assertIn("invalid_startup_input", buf.getvalue())
            self.assertNotIn("generate_executor_invalid", buf.getvalue())
        finally:
            if prev is not None:
                os.environ["ATMOFAB_GENERATE_EXECUTOR"] = prev
            else:
                os.environ.pop("ATMOFAB_GENERATE_EXECUTOR", None)



# ======================================================================================
# The `harness` bundle shape (issue #169): an `infrastructure` node's self-test
# ======================================================================================
_HARNESS_SPEC_PATH = "spec/infrastructure/infra/harness/harness_fortran_cpu"
_HARNESS_SAFE = wc.node_key_safe(_HARNESS)
_HARNESS_OPS = ("harness_fortran_cpu__parse_cases", "harness_fortran_cpu__emit_real")


def _harness_ir() -> dict:
    return {
        "meta": {"spec_id": _HARNESS_SPEC_ID, "spec_kind": "infrastructure",
                 "node_key": _HARNESS},
        "impl_defaults": {
            "toolchain": {"language": "fortran", "standard": "f2008", "build_system": "make"},
            "target": {"backend": "openmp"},
        },
        "algorithm": {"state_variables": []},
        "dependency": {"direct_deps": []},
        "case": {"test_case_set": [{"case_id": "c1"}]},
        "public_api": {
            "published_operations": [{"operation_id": op} for op in _HARNESS_OPS],
        },
        "io_contract": {
            "raw_requirements": {"required_evidence": []},
            "test_evidence_requirements": [{"test_id": "c1", "required_raw_variables": []}],
            "diagnostics_contract": {"checks": [{"id": "plumbing"}]},
            "test_predicates": [
                {"test_id": "c1", "expected_outcome": "pass", "target_cases": ["c1"]}],
        },
    }


def _write_harness_node(repo: Path, *, ir_id="h_20260908_001",
                        source_id="src_20260908_001") -> wc.NodeRefs:
    """The harness node's IR + sidecar + spec documents. NO runner is staged: on this shape the
    host renders none — that is what makes it the second shape rather than an M3c node."""
    import yaml
    ir_dir = repo / "workspace" / "ir" / _HARNESS_SAFE / ir_id
    ir_dir.mkdir(parents=True, exist_ok=True)
    (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(_harness_ir()), encoding="utf-8")
    (ir_dir / "dependency_graph.json").write_text(
        json.dumps({"all_nodes": [
            {"node_key": _HARNESS, "topo_level": 0, "direct_deps": []}]}), encoding="utf-8")
    spec_dir = repo / _HARNESS_SPEC_PATH
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "tests.md").write_text("- test: the plumbing round-trips\n", encoding="utf-8")
    (spec_dir / "controlled_spec.md").write_text("## 5 Algorithm\nplumbing\n", encoding="utf-8")
    return wc.NodeRefs(node_key=_HARNESS, spec_path=_HARNESS_SPEC_PATH, ir_id=ir_id,
                       pipeline_id="h_20260908_001", source_id=source_id)


def _harness_bundle() -> dict:
    model = (f"module {_HARNESS_SPEC_ID}_model\n! model\nend module {_HARNESS_SPEC_ID}_model\n")
    return {
        "bundle_schema_version": "1.1.0",
        "optimization_unit": {"members": [_HARNESS]},
        "files": [
            {"logical_path": f"{_HARNESS_SPEC_ID}_model.f90", "role": "model",
             "language": "fortran", "member_node_key": _HARNESS, "content": model,
             "modules": [f"{_HARNESS_SPEC_ID}_model"]},
            {"logical_path": f"{_HARNESS_SPEC_ID}_runner.f90", "role": "runner",
             "language": "fortran", "member_node_key": _HARNESS,
             "content": f"program {_HARNESS_SPEC_ID}_runner\nend program\n", "modules": []},
        ],
        "entrypoints": [
            {"symbol": op, "kind": "operation", "node_key": _HARNESS,
             "defined_in": f"{_HARNESS_SPEC_ID}_model.f90",
             "module": f"{_HARNESS_SPEC_ID}_model"} for op in _HARNESS_OPS],
        "target_lowering_plan": {"precision": {"real_kind": "real64"},
                                 "state_residency": "host"},
        "capability_requirements": ["sync_single_case@1"],
    }


class PureHarnessProducerEndToEndTests(unittest.TestCase):
    """The whole host side of one harness-shape `generate.generate` turn, driven through the
    production loop: launch request -> envelope -> acceptance -> host writes. What the m3c
    producer tests do for their shape, for this one."""

    _REPO_DOCS = ("docs/workflow/RUNNER_OUTPUT_CONTRACT.md",
                  "docs/workflow/CHECKS_MODULE_CONTRACT.md",
                  "docs/workflow/phases/phase_02_generate.md")

    def setUp(self) -> None:
        import shutil
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        real_root = Path(wc.__file__).resolve().parents[1]
        for rel in self._REPO_DOCS:
            dest = self.repo / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(real_root / rel, dest)
        self.refs = _write_harness_node(self.repo)
        self.c = _conductor(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_one_accepted_turn_writes_the_declared_sources_and_the_control_file(self) -> None:
        self.c.envelopes = [_envelope(_harness_bundle())]
        oc = self.c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(oc.status, "pass", getattr(oc, "failure_excerpt", None))
        src = self.repo / self.refs.source_dir() / "src"
        for entry in _harness_bundle()["files"]:
            self.assertEqual((src / entry["logical_path"]).read_text(encoding="utf-8"),
                             entry["content"])
        control = (src / self.c.CONTROL_FILE_BASENAME).read_text(encoding="utf-8")
        self.assertIn(f"$(OBJDIR)/{_HARNESS_SPEC_ID}_model.o", control)
        self.assertIn(f"$(OBJDIR)/{_HARNESS_SPEC_ID}_runner.o", control)
        self.assertIn(f"BIN ?= {_HARNESS_SPEC_ID}_runner", control)
        gen = self.repo / self.refs.source_dir()
        self.assertEqual(json.loads((gen / "codegen_bundle.json").read_text())["files"][1]["role"],
                         "runner")
        self.assertEqual(json.loads((gen / "bundle_meta.json").read_text())["result"], "pass")

    def test_the_launch_carried_the_shape_the_template_and_the_harness_context(self) -> None:
        self.c.envelopes = [_envelope(_harness_bundle())]
        self.c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        request = [cap["--request-json"]
                   for sub, cap in self.c.calls if sub == "record-launch"][-1]
        self.assertEqual(request["pure_shape"], "harness")
        self.assertEqual(request["leaf_mode"], "pure")
        self.assertNotIn("runner_host_authored", request)
        self.assertEqual(
            sorted(request["pure_context"]),
            sorted(ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[
                ("generate", "generate", "harness")]))
        # ...and the prompt the runtime renders for it is the harness template, not the default.
        prompt = ort.render_launch_prompt_text(
            ort.prepare_launch_request_payload(request))
        self.assertIn("bundle shape is `harness`", prompt)

    def test_a_wrong_shape_bundle_is_repaired_rather_than_written(self) -> None:
        """The acceptance layer runs on this shape too: an m3c-shaped bundle (a checks file, no
        runner) is refused, the loop repairs, and the accepted second attempt is what lands."""
        wrong = _harness_bundle()
        wrong["files"] = [wrong["files"][0], {
            "logical_path": f"{_HARNESS_SPEC_ID}_checks.f90", "role": "checks",
            "language": "fortran", "member_node_key": _HARNESS,
            "content": "module c\nend module c\n",
            "modules": [f"{_HARNESS_SPEC_ID}_checks"]}]
        self.c.envelopes = [_envelope(wrong), _envelope(_harness_bundle())]
        oc = self.c._run_pure_generate_substep(self.refs, "generate", "generate", None, ())
        self.assertEqual(oc.status, "pass", getattr(oc, "failure_excerpt", None))
        src = self.repo / self.refs.source_dir() / "src"
        self.assertTrue((src / f"{_HARNESS_SPEC_ID}_runner.f90").exists())
        self.assertFalse((src / f"{_HARNESS_SPEC_ID}_checks.f90").exists())
        meta = json.loads(
            (self.repo / self.refs.source_dir() / "bundle_meta.json").read_text())
        self.assertEqual(meta["attempts"], 2)
        self.assertEqual(meta["per_attempt"][0]["failure_category"], "bundle_shape_unsupported")


class PureHarnessShapeTests(unittest.TestCase):
    """The host side of the second bundle shape: what the leaf is shown, what it is judged
    against, and what the assembly declares as host glue."""

    #: The repository documents the harness contexts inline. Copied into the fixture repo the
    #: way `test_pure_leaf_verify` seeds the checks contract: the builders read them off
    #: `repo_root`, so the fixture's throwaway root has to carry them.
    _REPO_DOCS = ("docs/workflow/RUNNER_OUTPUT_CONTRACT.md",
                  "docs/workflow/CHECKS_MODULE_CONTRACT.md",
                  "docs/workflow/phases/phase_02_generate.md")

    def setUp(self) -> None:
        import shutil
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        real_root = Path(wc.__file__).resolve().parents[1]
        for rel in self._REPO_DOCS:
            dest = self.repo / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(real_root / rel, dest)
        self.refs = _write_harness_node(self.repo)
        self.c = _conductor(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_node_takes_the_harness_shape_and_both_generate_pairs_go_pure(self) -> None:
        self.assertEqual(self.c._bundle_shape(self.refs), "harness")
        self.assertTrue(self.c._pure_leaf_substep(self.refs, "generate", "generate"))
        self.assertTrue(self.c._pure_leaf_substep(self.refs, "generate", "verify"))

    def test_the_producer_context_carries_the_output_contract_not_a_runner(self) -> None:
        ctx = self.c._build_pure_harness_context(self.refs)
        self.assertEqual(
            sorted(ctx),
            sorted(ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[
                ("generate", "generate", "harness")]))
        self.assertNotIn("runner_document", ctx)
        # The document is inlined WHOLE, not sliced: a runner-authoring leaf reads all of it.
        contract = (Path(wc.__file__).resolve().parents[1]
                    / "docs" / "workflow" / "RUNNER_OUTPUT_CONTRACT.md").read_text(
                        encoding="utf-8")
        self.assertEqual(ctx["runner_output_contract_document"], contract)
        # ...and the manifest it is shown is its OWN.
        shown = json.loads(ctx["harness_capabilities"])
        self.assertEqual([m["node_key"] for m in shown["manifests"]], [_HARNESS])

    def test_the_lint_rule_set_reaches_the_leaf_from_the_BACKEND(self) -> None:
        """The rules §5 does not state (round 4 measured four of seven missing) reach the leaf
        only here, and they come from the backend that IMPOSES them rather than from prose in a
        neutral-core template — which is the placement `docs/BACKEND_BOUNDARY.md` prescribes and
        the reason they were absent.

        Pinned by IDENTITY against the backend's own renderer and by the property that matters:
        every code the gate selects is named in what the leaf is shown. A test that only checked
        non-emptiness is what let the previous document ship unnoticed."""
        from tools.backends.linter.fortitude import lint as fortitude
        ctx = self.c._build_pure_harness_context(self.refs)
        self.assertEqual(ctx["lint_rules_document"], fortitude.lint_rules_document())
        for code in fortitude.RULE_CODES:
            self.assertIn(code, ctx["lint_rules_document"], code)
            self.assertIn(fortitude.RULE_NAMES[code], ctx["lint_rules_document"], code)
        # The four this branch's round 4 measured as unreachable, named so a reader can see the
        # defect this row closes rather than re-deriving it.
        for code in ("C011", "C122", "C131", "PORT011"):
            self.assertIn(code, ctx["lint_rules_document"])

    def test_the_lint_rule_set_fails_CLOSED_when_the_backend_cannot_answer(self) -> None:
        """Every branch of the resolution raises a NAMED reason rather than degrading: a leaf
        that is not shown the rule set cannot satisfy it, so an empty slot is the defect, not a
        smaller prompt. The caller turns each into `pure_context_assembly_failed`."""
        from tools.backends.linter.fortitude import lint as fortitude
        with mock.patch.dict(
                "tools.validate_pipeline_semantics._LINT_PRESET_FOR_LANGUAGE", {}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                self.c._build_pure_harness_context(self.refs)
        self.assertIn("pure_lint_rules_document_unavailable", str(caught.exception))
        with mock.patch.object(fortitude, "lint_rules_document", lambda: "   "):
            with self.assertRaises(RuntimeError) as caught:
                self.c._build_pure_harness_context(self.refs)
        self.assertIn("pure_lint_rules_document_unavailable", str(caught.exception))
        with mock.patch.object(fortitude, "lint_rules_document", None):
            with self.assertRaises(RuntimeError) as caught:
                self.c._build_pure_harness_context(self.refs)
        self.assertIn("states no declared rule set", str(caught.exception))
        # The FOURTH branch, which a round-5 sweep found unwitnessed while the commit message
        # asserted "four named failure modes, all RAISING": a preset whose package does not
        # DECLARE the `lint_rules` capability. Replacing the registry refusal with a silent
        # fallback to fortitude survived every test file until this row.
        with mock.patch.dict("tools.validate_pipeline_semantics._LINT_PRESET_FOR_LANGUAGE",
                             {"fortran": "cppcheck"}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                self.c._build_pure_harness_context(self.refs)
        self.assertIn("pure_lint_rules_document_unavailable", str(caught.exception))
        self.assertIn("cppcheck", str(caught.exception))

    def test_the_lint_rule_set_is_resolved_from_THIS_NODE_S_language(self) -> None:
        """The docstring's load-bearing claim — the leaf is shown the rules its own source will
        be checked against — rests on the language being READ, and a round-5 sweep found
        hardcoding it to `"fortran"` survived every test file. The preset table is keyed by
        language, so the witness varies the language and requires the resolution to follow."""
        import yaml
        ir_path = self.repo / self.refs.ir_ref / "spec.ir.yaml"
        ir = yaml.safe_load(ir_path.read_text(encoding="utf-8"))
        ir["impl_defaults"]["toolchain"]["language"] = "python"
        ir_path.write_text(yaml.safe_dump(ir), encoding="utf-8")
        # `python` resolves to a linter that declares no `lint_rules`, so a resolution that
        # followed the node would refuse — and one that ignored it would answer fortitude's set.
        with self.assertRaises(RuntimeError) as caught:
            self.c._lint_rules_document(self.refs)
        self.assertIn("pure_lint_rules_document_unavailable", str(caught.exception))
        self.assertIn("ruff", str(caught.exception))

    def test_the_gate_guards_slice_is_SECTION_5_and_not_another(self) -> None:
        """CONTENT equality, not non-emptiness. A round-4 sweep found every new refusal of the
        round-3 fix unwitnessed, and the sharpest mutant was swapping the slicer for
        `_checks_contract_abi_sections` — handing the leaf §1-§4, the checks-module ABI this
        shape has no file for and the slicer's docstring says is excluded. Nothing noticed,
        because the only assertion was that the value was non-empty."""
        ctx = self.c._build_pure_harness_context(self.refs)
        real = (Path(wc.__file__).resolve().parents[1]
                / "docs" / "workflow" / "CHECKS_MODULE_CONTRACT.md").read_text(encoding="utf-8")
        self.assertEqual(ctx["gate_guards_document"],
                         wc._checks_contract_gate_guards_section(real))
        self.assertNotEqual(ctx["gate_guards_document"],
                            wc._checks_contract_abi_sections(real))

    def test_the_gate_guards_slicer_refuses_a_section_appended_after_it(self) -> None:
        """The slicer runs to the END of its document, so a `## 6.` added above it would widen
        the slice silently — which is what every other slice's literal end anchor prevents. It
        raises instead, and BOTH refusals are driven here: a round-4 sweep deleted each of them
        in turn and the whole suite stayed green, while `test_pure_prompt_contract_drift`'s
        comment claimed the digest pinned them (the digest pins the slice's CONTENT under
        today's document, which has no §6 — it cannot see the guard).

        Driven on synthetic text, because the real document must not carry a §6 for this to be
        checkable, and on the real document, so the two cannot diverge."""
        real = (Path(wc.__file__).resolve().parents[1]
                / "docs" / "workflow" / "CHECKS_MODULE_CONTRACT.md").read_text(encoding="utf-8")
        self.assertTrue(wc._checks_contract_gate_guards_section(real).startswith("## 5."))
        with self.assertRaises(ValueError) as later:
            wc._checks_contract_gate_guards_section(real + "\n## 6. Appendix\n\nbody\n")
        self.assertIn("numbered section after", str(later.exception))
        with self.assertRaises(ValueError) as absent:
            wc._checks_contract_gate_guards_section(
                real.replace("## 5. ", "## Five ", 1))
        self.assertIn("no '## 5.' section heading", str(absent.exception))
        # A SUBSECTION is not a section: `## 5.1` must stay inside the slice, or a document
        # that grows a subsection loses everything after it.
        widened = wc._checks_contract_gate_guards_section(
            real + "\n## 5.1 More guards\n\nbody\n")
        self.assertIn("## 5.1 More guards", widened)

    def test_the_gate_guards_read_fails_CLOSED_in_both_directions(self) -> None:
        """The two RAISING reads the context builder added. Same shape as the reviewer's row in
        `test_pure_leaf_verify`, and written because round 3 claimed both were driven and
        neither was: soft-failing either one to `""` survived seven test files."""
        target = self.repo / "docs" / "workflow" / "CHECKS_MODULE_CONTRACT.md"
        body = target.read_text(encoding="utf-8")
        target.unlink()
        try:
            with self.assertRaises(RuntimeError) as caught:
                self.c._build_pure_harness_context(self.refs)
            self.assertIn("pure_gate_guards_document_missing", str(caught.exception))
        finally:
            target.write_text(body, encoding="utf-8")
        target.write_text(body.replace("## 5. ", "## Five ", 1), encoding="utf-8")
        try:
            with self.assertRaises(RuntimeError) as caught:
                self.c._build_pure_harness_context(self.refs)
            self.assertIn("pure_gate_guards_document_unsliceable", str(caught.exception))
        finally:
            target.write_text(body, encoding="utf-8")

    def test_the_producer_context_raises_when_the_contract_is_unreadable(self) -> None:
        """The disposition the m3c producer's runner read has: a document the leaf cannot repair
        makes fail_closed the correct terminus, and the caller turns this into
        `pure_context_assembly_failed` with no leaf spawned. Degrading to `""` would defer the
        refusal one frame into `record_launch` and abort the conductor instead."""
        (self.repo / "docs/workflow/RUNNER_OUTPUT_CONTRACT.md").unlink()
        with self.assertRaises(RuntimeError) as caught:
            self.c._build_pure_harness_context(self.refs)
        self.assertIn("pure_runner_output_contract_document_missing", str(caught.exception))
        with self.assertRaises(RuntimeError) as caught2:
            self.c._build_pure_harness_verify_context(self.refs)
        self.assertIn("pure_runner_output_contract_document_missing", str(caught2.exception))

    def test_the_verify_context_carries_the_output_contract_not_the_checks_abi(self) -> None:
        ctx = self.c._build_pure_harness_verify_context(self.refs)
        self.assertEqual(
            sorted(ctx),
            sorted(ort.PURE_CONTEXT_REQUIRED_KEYS_BY_SHAPE[("generate", "verify", "harness")]))
        self.assertNotIn("checks_module_contract_document", ctx)

    def test_the_assembly_declares_no_host_glue(self) -> None:
        graph = self.c._build_pure_bundle_graph(self.refs, _harness_bundle())
        sources = [str(u["source"]) for u in graph["compile_units"]]
        self.assertEqual([s for s in sources if s.startswith("glue:")], [])
        self.assertIn(f"bundle:{_HARNESS_SPEC_ID}_runner.f90", sources)
        # ...and the runner's object is still LINKED — an empty glue set must not drop it.
        self.assertIn(f"{_HARNESS_SPEC_ID}_runner.o", graph["link"]["objects"])
        # The m3c node's graph still declares its glue, so this is a shape difference and not a
        # blanket removal.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            m3c = _conductor(repo)._build_pure_bundle_graph(refs, _valid_bundle())
        self.assertIn(f"glue:{_SPEC_ID}_runner.f90",
                      [str(u["source"]) for u in m3c["compile_units"]])

    def test_the_acceptance_layer_takes_the_harness_shape(self) -> None:
        self.assertIsNone(self.c._pure_bundle_violations(self.refs, _harness_bundle()))

    def test_a_checks_bearing_bundle_is_refused_on_this_shape(self) -> None:
        doc = _harness_bundle()
        doc["files"].append(
            {"logical_path": f"{_HARNESS_SPEC_ID}_checks.f90", "role": "checks",
             "language": "fortran", "member_node_key": _HARNESS,
             "content": "module x\nend module x\n",
             "modules": [f"{_HARNESS_SPEC_ID}_checks"]})
        result = self.c._pure_bundle_violations(self.refs, doc)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "bundle_shape_unsupported")

    def test_a_runner_role_is_refused_on_the_m3c_shape(self) -> None:
        """The other direction, driven through the CONDUCTOR so the shape resolution is the
        production one: an M3c node's bundle may not carry the role at all."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = _conductor(repo)
            doc = _valid_bundle()
            doc["files"].append(
                {"logical_path": f"{_SPEC_ID}_extra_runner.f90", "role": "runner",
                 "language": "fortran", "member_node_key": _NODE,
                 "content": "program p\nend program\n", "modules": []})
            result = c._pure_bundle_violations(refs, doc)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "bundle_shape_unsupported")
        self.assertIn("host-rendered glue", result[1])

    def test_the_launch_request_stamps_the_shape_and_the_real_authorship(self) -> None:
        spec = self.c._pure_producer_spec("generate", self.c._bundle_shape(self.refs))
        self.assertEqual(spec.pure_shape, "harness")
        self.assertFalse(spec.wants_exemplar)
        makefile_ha, runner_ha = spec.host_authored_flags(self.refs)
        self.assertTrue(makefile_ha)
        self.assertFalse(runner_ha)  # the host renders no runner on this shape
        req = wc.build_launch_request(
            self.refs, step="generate", substep="generate", orchestration_id="o",
            orchestration_agent_run_id="p", child_agent_run_id="c", agent_model="m",
            workflow_mode="dev", makefile_host_authored=makefile_ha,
            runner_host_authored=runner_ha, pure_leaf=True,
            pure_shape=spec.pure_shape,
            pure_context=self.c._build_pure_harness_context(self.refs))
        self.assertEqual(req["pure_shape"], "harness")
        self.assertNotIn("runner_host_authored", req)
        ort._validate_launch_request_payload(ort.prepare_launch_request_payload(req))

    def test_the_reviewer_spec_takes_the_harness_shape(self) -> None:
        spec = self.c._pure_reviewer_spec("generate", "verify", "harness")
        self.assertEqual(spec.pure_shape, "harness")
        self.assertIs(spec.build_context.__func__,
                      wc.Conductor._build_pure_harness_verify_context)

    def test_the_tamper_gate_admits_no_undeclared_source_on_this_shape(self) -> None:
        """C8: the `m3c` carve-out for the host-rendered runner is CLOSED here — the runner is
        declared bundle content, so an undeclared source beside it is a provenance violation."""
        doc = _harness_bundle()
        gen = self.repo / self.refs.source_dir()
        src = gen / "src"
        src.mkdir(parents=True, exist_ok=True)
        for entry in doc["files"]:
            (src / entry["logical_path"]).write_text(entry["content"], encoding="utf-8")
        (gen / "codegen_bundle.json").write_text(json.dumps(doc), encoding="utf-8")
        v: list[str] = []
        vps._validate_post_generate_bundle(self.repo, gen, _HARNESS, self.refs.ir_ref, v)
        self.assertEqual(v, [])
        # The runner is DECLARED, so removing it from files[] makes the staged file undeclared.
        doc2 = _harness_bundle()
        doc2["files"] = [e for e in doc2["files"] if e["role"] != "runner"]
        (gen / "codegen_bundle.json").write_text(json.dumps(doc2), encoding="utf-8")
        v2: list[str] = []
        vps._validate_post_generate_bundle(self.repo, gen, _HARNESS, self.refs.ir_ref, v2)
        self.assertTrue([x for x in v2 if "bundle_shape_unsupported" in x], v2)

    def test_the_tamper_gate_refuses_a_bundle_on_a_shapeless_node(self) -> None:
        """`shape=(shape or "")` is the gate's fail-CLOSED default, and a round-2 sweep found it
        unpinned: `shape=(shape or "m3c")` survived every test file.

        The mutation direction is fail-OPEN, and the node it matters on is exactly the node
        whose Generate falls through to the AGENTIC leaf — which holds filesystem tools and can
        therefore stage a `codegen_bundle.json` of its own. This refusal is the only thing
        between that document and an acceptance layer applying another shape's rules to it."""
        import yaml
        ir_path = self.repo / self.refs.ir_ref / "spec.ir.yaml"
        ir = yaml.safe_load(ir_path.read_text(encoding="utf-8"))
        # A toolchain the neutral core writes no control file for: both readers answer None.
        ir["impl_defaults"]["toolchain"]["language"] = "zz_lang"
        ir_path.write_text(yaml.safe_dump(ir), encoding="utf-8")
        self.assertIsNone(self.c._bundle_shape(self.refs))
        self.assertIsNone(vps._ir_bundle_shape(ir, self.refs.node_key))

        doc = _harness_bundle()
        gen = self.repo / self.refs.source_dir()
        src = gen / "src"
        src.mkdir(parents=True, exist_ok=True)
        for entry in doc["files"]:
            (src / entry["logical_path"]).write_text(entry["content"], encoding="utf-8")
        (gen / "codegen_bundle.json").write_text(json.dumps(doc), encoding="utf-8")
        v: list[str] = []
        vps._validate_post_generate_bundle(self.repo, gen, _HARNESS, self.refs.ir_ref, v)
        self.assertTrue([x for x in v if "unknown bundle shape ''" in x], v)

    def test_the_two_gates_resolve_the_same_shape_and_glue(self) -> None:
        """The producer's acceptance and the tamper gate must judge one bundle by one shape."""
        import yaml
        ir = yaml.safe_load(
            (self.repo / self.refs.ir_ref / "spec.ir.yaml").read_text(encoding="utf-8"))
        self.assertEqual(self.c._bundle_shape(self.refs),
                         vps._ir_bundle_shape(ir, self.refs.node_key))


if __name__ == "__main__":
    unittest.main()
