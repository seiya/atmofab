"""The CUDA C++ backend's `runner_render` capability: a physics node's host-authored runner glue
(issue #289, R4-b PR-6).

Pure-function module reached through `registry.capability_module("language", "cuda_cpp",
"runner_render")` — the neutral seam is `tools/host_render.py`, which is what the conductor and
the compile gate call. Nothing outside this package imports it by name.

It renders two files of a `component` / `problem` node from the node's IR:

* ``<spec_id>_runner.cu`` (`render_runner`) — the deterministic main program that drives the
  node's ``<spec_id>_checks`` callbacks and emits the standard runner outputs THROUGH the
  certified ``harness_cpp_gpu`` plumbing, which owns the JSON envelope assembly and the verdict
  fold (harness controlled_spec §3 / §5.1). The runner holds no serialization knowledge: it builds
  the harness's record types and calls the writers — it never formats a JSON token, folds a
  verdict, or excludes an xfail itself.
* ``<spec_id>_checks.cuh`` (`render_checks_header`) — the DECLARATIONS of the checks ABI: the five
  callbacks and one ``extern`` per bound state variable, in namespace ``<spec_id>_checks``. The
  leaf's ``<spec_id>_checks.cu`` includes it and DEFINES every name it declares.

WHY THE HOST DECLARES THE CHECKS ABI. The runner and the checks source are separate translation
units that meet at link, and C++ does not check a namespace-scope VARIABLE's type across them: the
mangled name of a variable carries no type, so a runner that declared ``extern std::vector<double>
u;`` itself would link against a leaf's ``std::vector<float> u;`` and read garbage — a silent ODR
violation. With the declaration rendered once and included by both files, a definition of another
type is a compile error in the checks source (nvcc 13.4: "declaration is incompatible with ...
declared at line N of <spec_id>_checks.cuh"), which the `Generate.gate` lint and syntax stages
refuse. A callback defined with
other parameter types is an overload the header's declaration never reaches — a link error — which
the checks gates refuse before that (`source.checks_module_abi_facts`).

Split of authorship on a physics node:
- ``<spec_id>_model.cu`` — the physics kernel + the published operation   (LLM leaf)
- ``<spec_id>_checks.cu`` — the checks callbacks + the bound state storage (LLM leaf)
- ``<spec_id>_runner.cu`` and ``<spec_id>_checks.cuh`` — this renderer     (host)
- ``<spec_id>_model.cuh`` — the published-surface header (`header.py`)     (host)
- ``src/Makefile`` — the build system's control-file renderer               (host)

The runner captures the bound state (Z6, issue #255): every snapshot variable is a namespace-scope
variable of ``<spec_id>_checks`` named exactly as the IR declares it, read by the runner as
``<spec_id>_checks::<var>`` and serialized by the harness emitters twice per case — right after
``case_setup`` and right after ``case_run`` — before any check or metric callback of that case
runs. A scalar is a ``double``, a rank-1 array a ``std::vector<double>``, a rank-R array an
``atmofab::Array<double, R>`` (the owning column-major array the published-surface header defines);
the runner hands the emitters a non-owning ``atmofab::View`` over it.

The language-neutral IR readers — the snapshot schema, the cases, the checks, the metrics basis,
and their refusals — are `tools/runner_ir.py`'s, shared with every backend that renders a runner.
This module adds the refusals CUDA C++ imposes on a name it binds (`_check_snapshot_name`). It
imposes no rendered-line width: the declared lint rule set of this language reads no column
(`tools/backends/linter/nvcc/lint.py`), so a width bound here would refuse IR the lint accepts.

``render_runner`` raises ``RenderError`` (transport fail_closed, never a Generate retry) for an IR
it cannot faithfully render; ``ir_content_violations`` reports the same refusals at Compile.
``assert_harness_pin`` is the separate fail-closed guard the conductor runs against the certified
harness IR signatures + source before rendering.
"""

from __future__ import annotations

import re
from typing import Any

from tools import runner_ir
from tools.backends.language.cuda_cpp import bundle, checks_abi
from tools.backends.language.cuda_cpp import declarations as cpp_decls
from tools.backends.language.cuda_cpp import header as cpp_header
from tools.backends.language.cuda_cpp import signatures as cpp_signatures
from tools.host_execution import perf_parallelism

# `RenderError` is the seam's class, not this module's: importing it is what makes the neutral
# `except RenderError:` clause in the conductor work (see `tools/host_render.py`).
from tools.host_render import RenderError

#: The fixed ABI of the leaf-authored checks source and its C++ declarations — `checks_abi`'s,
#: re-exported here because the seam reads the names off the module that renders the runner
#: (`host_render.checks_public_names`).
CHECKS_PUBLIC_NAMES = checks_abi.CHECKS_PUBLIC_NAMES
CHECKS_ABI_PARAMS = checks_abi.CHECKS_ABI_PARAMS

#: The width the neutral contract gives a check status (`pass` / `fail` / `na` right-padded to
#: it). A C++ status is a `std::string`, so nothing is truncated to it; the not-applicable status
#: is written `"na  "` as the contract states it, and the harness trims the padding before it
#: writes the status (`trim_status` in the certified model), so `diagnostics.json` is one document
#: across targets.
CHECK_STATUS_WIDTH = 4

#: The harness's `case_id_len` — the neutral reader's bound on a declared case id, restated as
#: the value the runner pins the certified harness header to (`static_assert`, see
#: `render_runner`).
CASE_ID_LEN = runner_ir.CASE_ID_LEN

#: The bound on a spec_id, derived as the Fortran backend derives its own: the longest name
#: generated from a spec_id appends a 7-character role suffix, plus one character of margin.
MAX_SPEC_ID_LEN = bundle.IDENTIFIER_MAX - len("_runner") - 1

#: Names a snapshot variable may not take although they are identifiers: the two namespaces the
#: checks header names inside `<spec_id>_checks` (a variable of that name declared there hides the
#: namespace from every later declaration of the header).
_NAMESPACE_NAMES = frozenset({"std", "atmofab"})

_IDENTIFIER_RE = re.compile(bundle.IDENTIFIER_PATTERN)

#: The harness symbols this template calls, pinned by `assert_harness_pin` against the certified
#: harness IR and source. Emitters are added per rank on demand (`_used_harness_ops`).
_HARNESS_TYPES = (
    "h_named", "h_check", "h_metric", "h_case_result", "h_mb_entry",
)
_HARNESS_CORE_OPS = (
    "parse_cases", "box", "write_snapshot",
    "write_metrics_basis", "write_diagnostics", "write_perf",
)

#: The target a DRY render is given: `ir_content_violations` renders only to learn whether the
#: IR renders, and discards the text. Both values reach nothing but the perf-record line, which
#: raises for no value, so they decide no violation.
_DRY_RUN_TARGET: dict[str, Any] = {"hardware": {"class": "gpu"},
                                   "execution": {"threads_per_rank": 1}}


# --- the IR, read through the neutral readers ---------------------------------------------------


def _check_snapshot_name(name: str) -> None:
    """The CUDA C++ rules on a snapshot variable's name: it IS a namespace-scope variable of
    `<spec_id>_checks`, declared by the checks header and read by the runner as
    `<spec_id>_checks::<name>`, so it must be an identifier of the bundle's grammar, not a
    keyword, not one of the ABI callbacks (a namespace cannot hold a variable and a function of
    one name) and not a namespace the header names."""
    if not _IDENTIFIER_RE.fullmatch(name):
        raise RenderError(
            f"snapshot variable {name!r} is not a bindable identifier: every snapshot variable "
            "is captured from a namespace-scope variable of that name in the checks source, so "
            f"it must match {bundle.IDENTIFIER_PATTERN}")
    if name in cpp_signatures.CPP_KEYWORDS:
        raise RenderError(
            f"snapshot variable {name!r} is a C++ keyword, so no checks source can declare a "
            "variable of that name — rename it (a snapshot variable must be declarable in every "
            "target language)")
    if name in CHECKS_PUBLIC_NAMES:
        raise RenderError(
            f"snapshot variable {name!r} collides with a checks-ABI callback name "
            f"{list(CHECKS_PUBLIC_NAMES)}; a namespace cannot hold a variable of that name "
            "beside the callback")
    if name in _NAMESPACE_NAMES:
        raise RenderError(
            f"snapshot variable {name!r} is the name of a namespace the checks header uses "
            f"({', '.join(sorted(_NAMESPACE_NAMES))}); declared in `<spec_id>_checks`, it would "
            "hide that namespace from the header's later declarations — rename it")


def _snapshot_schema(ir: dict[str, Any]) -> tuple[dict[str, str], str]:
    return runner_ir.snapshot_schema(ir, _check_snapshot_name)


def _bound_state(ir: dict[str, Any]) -> list[tuple[str, int]]:
    """Every snapshot variable with its rank, in schema declaration order."""
    schema_vars, _ = _snapshot_schema(ir)
    return [(v, runner_ir.rank_of_shape(shape, v)) for v, shape in schema_vars.items()]


def _state_type(rank: int) -> str:
    """The C++ type of a bound state variable of `rank` (CHECKS_ABI.md §1-b)."""
    if rank == 0:
        return "double"
    if rank == 1:
        return "std::vector<double>"
    return f"{cpp_signatures.ARRAY_TYPE}<double, {rank}>"


def _clit(value: str) -> str:
    """`value` as the contents of a C++ string literal.

    IR-sourced names (case ids, snapshot variable names, metric addresses, test ids) are only
    required to be non-empty by the compile gates, so each is escaped: `\\`, `"` and `?` (a `??`
    sequence would be a trigraph to a compiler that reads them, and a warning under the lint's
    `-Wall` where it does not). Everything embedded must be printable ASCII, as the Fortran
    renderer requires of its literals: a control character has no plain literal form, and the
    neutral bounds (`runner_ir.CASE_ID_LEN`) count code points."""
    bad = sorted({ch for ch in value if not (0x20 <= ord(ch) <= 0x7E)})
    if bad:
        raise RenderError(
            f"value {value!r} contains character(s) {bad!r} outside printable ASCII and cannot "
            "be embedded in the generated C++ source (a control character has no plain literal "
            "form; a non-ASCII character makes the byte length disagree with the code-point "
            "length every render bound is measured in)")
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("?", "\\?")


def _used_harness_ops(ir: dict[str, Any]) -> list[str]:
    """Unqualified harness op names the rendered glue calls (deterministic order): the core
    writers/plumbing plus only the emitters for the ranks in use."""
    ranks = {rank for _v, rank in _bound_state(ir)}
    ops = ["parse_cases"]
    if 0 in ranks:
        ops.append("emit_real")
    ops += [f"emit_array_r{r}" for r in sorted(r for r in ranks if r >= 1)]
    ops += ["box", "write_snapshot", "write_metrics_basis",
            "write_diagnostics", "write_perf"]
    return ops


def _hname(harness_spec_id: str, sym: str) -> str:
    return f"{harness_spec_id}__{sym}"


def _check_identifier_lengths(spec_id: str, harness_spec_id: str) -> None:
    # Node-IDENTITY defects (a re-author cannot shorten the spec_id / harness id), so
    # `identity=True`: the compile.static mirror excludes them (spec-input concern).
    if len(spec_id) > MAX_SPEC_ID_LEN:
        raise RenderError(
            f"spec_id {spec_id!r} is {len(spec_id)} chars (>{MAX_SPEC_ID_LEN}); the derived "
            f"`<spec_id>_checks` / `_runner` identifiers would exceed the "
            f"{bundle.IDENTIFIER_MAX}-char identifier limit", identity=True)
    for sym in (*_HARNESS_TYPES, *_HARNESS_CORE_OPS):
        name = _hname(harness_spec_id, sym)
        if len(name) > bundle.IDENTIFIER_MAX:
            raise RenderError(
                f"harness identifier {name!r} is {len(name)} chars (>{bundle.IDENTIFIER_MAX})",
                identity=True)


def ir_content_violations(ir: dict[str, Any], spec_id: str, harness_spec_id: str) -> list[str]:
    """The Compile-authored render preconditions of a physics node's host-rendered runner, as
    human-readable messages (``[]`` when the IR renders, or when the only defect is a node-identity
    one this deliberately excludes).

    An EXACT mirror by construction, as the Fortran backend's is: it invokes ``render_runner``
    itself with the ``(ir, spec_id, harness_spec_id)`` the conductor passes and reports whatever
    ``RenderError`` the render raises, ``identity=True`` ones excluded (they belong to spec-input
    validation). An exception of another class — a truthy non-iterable IR field — is reported as
    a violation too, never raised: this runs inside the compile validator, where an uncaught
    exception discards every violation the sibling gates collected."""
    try:
        render_runner(ir, spec_id, harness_spec_id, target=_DRY_RUN_TARGET)
    except RenderError as exc:
        return [] if exc.identity else [str(exc)]
    except Exception as exc:  # noqa: BLE001
        return [f"IR is not renderable ({type(exc).__name__}: {exc})"]
    return []


def _abi_declaration(name: str) -> str:
    params = ", ".join(f"{ptype} {pname}" for ptype, pname in CHECKS_ABI_PARAMS[name])
    return f"void {name}({params});"


def render_checks_header(ir: dict[str, Any], spec_id: str) -> tuple[str, str]:
    """`(basename, text)` of ``<spec_id>_checks.cuh``: the checks ABI's declarations — the five
    callbacks (`CHECKS_ABI_PARAMS`) and one ``extern`` per bound state variable of the IR's
    snapshot schema, typed by rank — in namespace ``<spec_id>_checks``, after the array types the
    published-surface header also defines (`header.VIEW_DEFINITION`, guarded, so either header
    may be included first). Deterministic and pure; raises ``RenderError`` for an IR
    ``render_runner`` refuses too."""
    spec_id = (spec_id or "").strip()
    if not spec_id:
        raise RenderError("spec_id is empty", identity=True)
    bound = _bound_state(ir)
    lines = [
        (f"// {checks_abi.checks_header_basename(spec_id)}: the checks ABI of {spec_id}, "
         "rendered by the host from the node's IR."),
        (f"// Do not edit: {bundle.checks_basename(spec_id)} includes this header and DEFINES "
         "every name it declares."),
        "#pragma once",
        "#include <string>",
        "#include <vector>",
        "",
        cpp_header.VIEW_DEFINITION.rstrip("\n"),
        "",
        f"namespace {spec_id}_checks {{",
        "// The five callbacks the host-rendered runner calls (CHECKS_ABI.md §1).",
    ]
    lines += [_abi_declaration(name) for name in CHECKS_PUBLIC_NAMES]
    lines.append("// The bound state the runner captures after case_setup and after case_run "
                 "(CHECKS_ABI.md §1-b).")
    lines += [f"extern {_state_type(rank)} {name};" for name, rank in bound]
    lines.append(f"}}  // namespace {spec_id}_checks")
    return checks_abi.checks_header_basename(spec_id), "\n".join(lines) + "\n"


def render_runner(ir: dict[str, Any], spec_id: str, harness_spec_id: str,
                  *, target: dict[str, Any]) -> str:
    """Render ``<spec_id>_runner.cu`` from the IR and the target. Deterministic and pure.

    ``harness_spec_id`` is the certified plumbing node's spec_id (``harness_cpp_gpu``). ``target``
    is the run's target profile document, read for the perf record's hardware class and
    parallelism only (`host_execution.perf_parallelism`). The returned text is the complete
    source (trailing newline included)."""
    if not isinstance(ir, dict):
        raise RenderError("IR is not a mapping", identity=True)
    spec_id = (spec_id or "").strip()
    harness_spec_id = (harness_spec_id or "").strip()
    if not spec_id:
        raise RenderError("spec_id is empty", identity=True)
    if not harness_spec_id:
        raise RenderError("harness_spec_id is empty", identity=True)
    _check_identifier_lengths(spec_id, harness_spec_id)
    infra = runner_ir.infra_dep_count(ir)
    if infra > 1:
        raise RenderError(
            f"node declares {infra} infrastructure dependencies; a physics node depends on "
            "exactly one harness (the runner glue is rendered against a single plumbing "
            "surface)", identity=True)

    # Everything from here down is Compile-authored IR content (`identity=False`), in the order
    # the Fortran renderer asks it, so an IR with several defects is told the same first one.
    schema_vars, time_var = _snapshot_schema(ir)
    runner_ir.require_time_variable_t(time_var)
    runner_ir.verify_verdict_fields(ir)
    case_ids = runner_ir.case_ids(ir)
    runner_ir.per_case_vars(ir, schema_vars)
    xfail = runner_ir.xfail_cases(ir)
    checks = runner_ir.check_ids(ir)
    metrics = runner_ir.metrics(ir)
    evidence = runner_ir.test_evidence(ir)
    target_class = runner_ir.target_class(target)
    ranks, threads, devices = perf_parallelism(target)
    bound = [(v, runner_ir.rank_of_shape(shape, v)) for v, shape in schema_vars.items()]
    array_bound = any(rank >= 1 for _v, rank in bound)

    hm = f"{harness_spec_id}_model"
    ck = f"{spec_id}_checks"

    def H(sym: str) -> str:  # local shorthand, as in the Fortran renderer
        return f"hm::{_hname(harness_spec_id, sym)}"

    lines: list[str] = []
    a = lines.append

    a(f"// {bundle.runner_basename(spec_id)}: rendered by the host from the node's IR. Do not "
      "edit.")
    a(f"// It drives the {ck} callbacks and emits the standard runner outputs THROUGH the")
    a(f"// certified {harness_spec_id} plumbing, which owns all JSON assembly and the verdict fold")
    a("// (harness controlled_spec §3 / §5.1). This glue holds no serialization knowledge: it")
    a("// builds harness records and calls the writers.")
    a(f'#include "{hm}.cuh"')
    a(f'#include "{checks_abi.checks_header_basename(spec_id)}"')
    a("")
    a("#include <chrono>")
    a("#include <cstddef>")
    a("#include <cstdio>")
    a("#include <cstdlib>")
    a("#include <iostream>")
    a("#include <string>")
    a("#include <type_traits>")
    a("#include <vector>")
    a("")
    a("namespace {")
    a("")
    a(f"namespace hm = {hm};")
    a(f"namespace ck = {ck};")
    a("")
    # The two §5.1 module parameters whose VALUES this glue relies on: the checks ABI passes
    # float64 values the harness takes as `dp`, and a declared case id is bounded by the
    # harness's `case_id_len` (`runner_ir.CASE_ID_LEN`). The signature pin compares prototypes,
    # which spell `dp` whatever it is; these make a drift of either value a compile error.
    a("static_assert(std::is_same<hm::dp, double>::value,")
    a(f'              "the certified {harness_spec_id} dp is not the float64 the checks ABI '
      'passes");')
    a(f"static_assert(hm::case_id_len == {CASE_ID_LEN},")
    a(f'              "the certified {harness_spec_id} case_id_len is not the width this runner '
      'bounds case ids by");')
    a("")
    a(f"using Named = hm::{_hname(harness_spec_id, 'h_named')};")
    a(f"using Check = hm::{_hname(harness_spec_id, 'h_check')};")
    a(f"using Metric = hm::{_hname(harness_spec_id, 'h_metric')};")
    a(f"using CaseResult = hm::{_hname(harness_spec_id, 'h_case_result')};")
    a(f"using MbEntry = hm::{_hname(harness_spec_id, 'h_mb_entry')};")
    a("")
    # Every exit of the program, the successful one included, goes through `finish`, which
    # flushes and ends the process with `std::_Exit`: no namespace-scope destructor and no exit
    # handler of the leaf's sources runs after the harness has written the run's outputs (round
    # 3 of this change's review — a C++ program otherwise runs them after `main` returns, and a
    # model source's destructor rewrote `diagnostics.json` there with every gate green).
    a("// Every exit ends here: flush, then end the process with no destructor or exit handler of")
    a("// the node's sources running after the harness has written the run's outputs.")
    a("[[noreturn]] void finish(int code) {")
    a("  std::cout.flush();")
    a("  std::cerr.flush();")
    a("  std::fflush(nullptr);")
    a("  std::_Exit(code);")
    a("}")
    a("")
    if array_bound:
        a("// Stop the run when a bound array does not hold its declared data at a capture point.")
        a("void require_bound(bool is_bound, const std::string& name, const std::string& cid) {")
        a("  if (is_bound) {")
        a("    return;")
        a("  }")
        a('  std::cerr << "error: bound state " << name << " is not allocated at capture for case "')
        a("            << cid << std::endl;")
        a("  finish(1);")
        a("}")
        a("")
    # Z6 capture contract (zero_base_architecture.md §A4): the harness serializes the BOUND
    # state — read straight from the checks namespace's storage — right after `case_setup` and
    # right after `case_run`, and the snapshot cache holds the serialized STRINGS, so nothing a
    # later callback writes into that storage can reach a snapshot or the metrics basis.
    a("// Every declared snapshot variable, read from the checks namespace's bound storage and")
    a("// serialized by the harness emitters. The same set for every case: the snapshot is the")
    a("// full declared state.")
    a("std::vector<Named> capture_state(const std::string& cid) {")
    if not array_bound:
        a("  (void)cid;")
    a("  std::vector<Named> out;")
    for name, rank in bound:
        vlit = _clit(name)
        storage = f"ck::{name}"
        if rank == 0:
            a(f'  out.push_back({H("box")}("{vlit}", {H("emit_real")}({storage})));')
        elif rank == 1:
            a(f'  require_bound(!{storage}.empty(), "{vlit}", cid);')
            a("  {")
            a(f"    const atmofab::View<const double, 1> view{{{storage}.data(),")
            a(f"                                               {{static_cast<long>({storage}.size())}}}};")
            a(f'    out.push_back({H("box")}("{vlit}", {H("emit_array_r1")}(view)));')
            a("  }")
        else:
            extents = [f"{storage}.extent[{k}]" for k in range(rank)]
            product = " * ".join(f"static_cast<std::size_t>({e})" for e in extents)
            positive = " && ".join(f"{e} > 0" for e in extents)
            a(f"  require_bound({positive} &&")
            a(f"                    {storage}.data.size() == {product},")
            a(f'                "{vlit}", cid);')
            a("  {")
            a(f"    const atmofab::View<const double, {rank}> view{{{storage}.data.data(),")
            a(f"                                               {{{', '.join(extents)}}}}};")
            a(f'    out.push_back({H("box")}("{vlit}", {H(f"emit_array_r{rank}")}(view)));')
            a("  }")
    a("  return out;")
    a("}")
    a("")
    a("// Index of `target` in the parsed case list, or -1 when absent.")
    a("int find_case_index(const std::vector<std::string>& ids, int n, const std::string& target) {")
    a("  for (int k = 0; k < n; ++k) {")
    a("    if (ids[static_cast<std::size_t>(k)] == target) {")
    a("      return k;")
    a("    }")
    a("  }")
    a("  return -1;")
    a("}")
    a("")
    a("// The boxed value named `name` from a case's cached snapshot values.")
    a("Named pick(const std::vector<Named>& values, const std::string& name) {")
    a("  for (const Named& nv : values) {")
    a("    if (nv.name == name) {")
    a("      return nv;")
    a("    }")
    a("  }")
    a('  std::cerr << "error: raw variable " << name << " absent from snapshot" << std::endl;')
    a("  finish(1);")
    a("}")
    a("")
    a("}  // namespace")
    a("")
    a("int main(int argc, char** argv) {")
    a("  // --- read argv and parse the case set (--cases <spec> <case_id>...) --------")
    a("  std::vector<std::string> tokens;")
    a("  for (int i = 1; i < argc; ++i) {")
    a("    tokens.emplace_back(argv[i]);")
    a("  }")
    a("  std::vector<std::string> case_ids;")
    a("  int ncases = 0;")
    a("  bool ok = false;")
    a(f"  {H('parse_cases')}(tokens, static_cast<int>(tokens.size()), case_ids, ncases, ok);")
    a("  if (!ok || ncases < 1 || static_cast<std::size_t>(ncases) > case_ids.size()) {")
    a('    std::cerr << "error: --cases <spec> <case_id>... required" << std::endl;')
    a("    finish(1);")
    a("  }")
    a("")
    a("  std::vector<CaseResult> results;")
    a("  std::vector<std::vector<Named>> snap_cache;")
    a("  int steps_total = 0;")
    a("  int cells_total = 0;")
    a("  const std::chrono::steady_clock::time_point clock0 = std::chrono::steady_clock::now();")
    a("")
    a("  for (int ci = 0; ci < ncases; ++ci) {")
    a("    const std::string cid = case_ids[static_cast<std::size_t>(ci)];")
    a("    bool setup_ok = false;")
    a("    ck::case_setup(cid, setup_ok);")
    a("")
    a("    // --- initial state: bound storage serialized right after case_setup, before any")
    a("    // --- callback of this case runs (raw/state_snapshots/initial/<case_id>.json) ---")
    a("    std::vector<Named> initial = capture_state(cid);")
    a("    double tval = 0.0;")
    a("    ck::get_time(tval);")
    a(f'    {H("write_snapshot")}("initial/" + cid, initial, tval);')
    a("")
    a("    int steps_c = 0;")
    a("    int cells_c = 0;")
    a("    bool run_ok = false;")
    a("    ck::case_run(cid, steps_c, cells_c, run_ok);")
    a("    steps_total += steps_c;")
    a("    cells_total += cells_c;")
    a("")
    a("    // --- final state: the same bound storage right after case_run, before any check")
    a("    // --- or metric callback of this case runs (raw/state_snapshots/<case_id>.json) ---")
    a("    std::vector<Named> final_state = capture_state(cid);")
    a("    ck::get_time(tval);")
    a(f"    {H('write_snapshot')}(cid, final_state, tval);")
    a("    snap_cache.push_back(final_state);")
    a("")
    a("    CaseResult result{};")
    a("    result.case_id = cid;")
    xf = [c for c in case_ids if c in xfail]
    xfail_expr = " || ".join(f'cid == "{_clit(c)}"' for c in xf) if xf else "false"
    a(f"    result.expected_xfail = {xfail_expr};")
    a("")
    a("    // --- honest per-case checks (runner-driven ids; the xfail fold is the harness's) ---")
    for cid_ in checks:
        clit = _clit(cid_)
        a("    {")
        a("      std::string cstatus;")
        a(f'      ck::checks_compute(cid, "{clit}", cstatus);')
        a("      Check check{};")
        a(f'      check.id = "{clit}";')
        a("      check.status = cstatus;")
        a("      result.checks.push_back(check);")
        a("    }")
    if metrics:
        a("")
        a("    // --- per-case metric leaves (dotted addresses; NA carried honestly) ---")
        for m in metrics:
            mlit = _clit(m)
            a("    {")
            a("      double mval = 0.0;")
            a("      bool mis_na = false;")
            a("      std::string mreason;")
            a("      bool mfound = false;")
            a(f'      ck::metric_compute(cid, "{mlit}", mval, mis_na, mreason, mfound);')
            a("      if (mfound) {")
            a("        Metric metric{};")
            a(f'        metric.name = "{mlit}";')
            a("        metric.value = mval;")
            a("        metric.is_na = mis_na;")
            a("        metric.reason_na = mis_na ? mreason : std::string();")
            a("        result.metrics.push_back(metric);")
            a("      }")
            a("    }")
    a("    results.push_back(result);")
    a("  }")
    a("")
    a("  double walltime = std::chrono::duration<double>(std::chrono::steady_clock::now() - clock0)")
    a("                        .count();")
    a("  if (walltime <= 0.0) {")
    a("    walltime = 1.0e-9;")
    a("  }")
    a("")
    mb_rows = runner_ir.metrics_basis_rows(ir, evidence, schema_vars)
    a("  // --- metrics-basis entries: one per (test_id, target case_id) --------------")
    a("  std::vector<MbEntry> mb_entries;")
    for tid, tcase, req_vars in mb_rows:
        tlit = _clit(tcase)
        a("  {")
        a(f'    const int tci = find_case_index(case_ids, ncases, "{tlit}");')
        a("    if (tci < 0) {")
        a(f'      std::cerr << "error: target case not run: " << "{tlit}" << std::endl;')
        a("      finish(1);")
        a("    }")
        a("    MbEntry entry{};")
        a(f'    entry.test_id = "{_clit(tid)}";')
        a(f'    entry.case_id = "{tlit}";')
        for rv in req_vars:
            a(f'    entry.values.push_back(pick(snap_cache[static_cast<std::size_t>(tci)], '
              f'"{_clit(rv)}"));')
        a("    mb_entries.push_back(entry);")
        a("  }")
    a("")
    a("  // --- emit the run outputs (the harness owns every envelope and the fold) -------")
    a(f"  {H('write_metrics_basis')}(mb_entries, {len(mb_rows)});")
    a(f"  {H('write_diagnostics')}(results, ncases);")
    a(f'  {H("write_perf")}(case_ids[static_cast<std::size_t>(ncases - 1)], '
      f'"{_clit(target_class)}", steps_total,')
    a(f"      cells_total, walltime, {ranks}, {threads}, {devices});")
    a("  finish(0);")
    a("}")
    return "\n".join(lines) + "\n"


# --- checks ABI: the dummy declaration the compiler cannot check ------------------------------


def checks_abi_dummy_violation(text: str, spec_id: str) -> str | None:
    """None, always. The Fortran backend reads one dummy attribute its compiler cannot check
    against the runner's call (issue #261). In CUDA C++ the checks source includes the
    host-rendered checks header, so a definition whose parameter types differ from the declared
    ABI is an overload the runner's call never reaches, which `source.checks_module_abi_facts`
    refuses by type; and every declared `out` argument is a reference to an object the runner
    owns, so no actual is handed over in a state a definition could mis-declare."""
    del text, spec_id
    return None


# --- harness interface signature pin (fail-closed, run before rendering) -----------------------
#
# The only harness this renderer targets is harness_cpp_gpu. The template is written against its
# §5.1 interface, embedded below VERBATIM in the neutral structured form the controlled spec
# states it in (Fortran's pin embeds Fortran text because it predates the neutral loader; this one
# is lowered with the same `signatures.render_symbol` the published-surface header is rendered
# with). `assert_harness_pin` checks that the certified harness the consumer will link against
# still publishes those exact declarations — in its IR `public_api.signatures` and in its
# generated model source — so a harness recert that silently changed the interface fails the
# consumer's render instead of miscompiling at Build.

EXPECTED_HARNESS_SPEC_ID = "harness_cpp_gpu"

# Verbatim copy of spec/infrastructure/infra/harness/harness_cpp_gpu/controlled_spec.md §5.1
# (spec_version 0.1.0). If a harness recert changes §5.1, THIS block and the render template must
# be updated together (the pin message says so).
_HARNESS_V1_INTERFACE = """\
module_parameters:
- name: dp
  value: float64
- name: case_id_len
  value: '64'
types:
- name: harness_cpp_gpu__h_named
  components:
  - name: name
    spec:
      type: string
      len: deferred
      alloc: true
  - name: json
    spec:
      type: string
      len: deferred
      alloc: true
- name: harness_cpp_gpu__h_check
  components:
  - name: id
    spec:
      type: string
      len: deferred
      alloc: true
  - name: status
    spec:
      type: string
      len: '4'
- name: harness_cpp_gpu__h_metric
  components:
  - name: name
    spec:
      type: string
      len: deferred
      alloc: true
  - name: value
    spec:
      type: real
      kind: dp
  - name: is_na
    spec:
      type: logical
  - name: reason_na
    spec:
      type: string
      len: deferred
      alloc: true
- name: harness_cpp_gpu__h_case_result
  components:
  - name: case_id
    spec:
      type: string
      len: deferred
      alloc: true
  - name: expected_xfail
    spec:
      type: logical
  - name: checks
    rank: 1
    spec:
      type: derived
      name: harness_cpp_gpu__h_check
      alloc: true
  - name: metrics
    rank: 1
    spec:
      type: derived
      name: harness_cpp_gpu__h_metric
      alloc: true
- name: harness_cpp_gpu__h_mb_entry
  components:
  - name: test_id
    spec:
      type: string
      len: deferred
      alloc: true
  - name: case_id
    spec:
      type: string
      len: deferred
      alloc: true
  - name: values
    rank: 1
    spec:
      type: derived
      name: harness_cpp_gpu__h_named
      alloc: true
procedures:
- kind: subroutine
  name: harness_cpp_gpu__parse_cases
  args:
  - name: tokens
    rank: 1
    intent: in
    spec:
      type: string
      len: assumed
  - name: ntokens
    intent: in
    spec:
      type: integer
  - name: case_ids
    rank: 1
    intent: out
    spec:
      type: string
      len: case_id_len
  - name: ncases
    intent: out
    spec:
      type: integer
  - name: ok
    intent: out
    spec:
      type: logical
- kind: function
  name: harness_cpp_gpu__emit_real
  args:
  - name: x
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_cpp_gpu__emit_int
  args:
  - name: i
    intent: in
    spec:
      type: integer
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_cpp_gpu__emit_bool
  args:
  - name: b
    intent: in
    spec:
      type: logical
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_cpp_gpu__emit_array_r1
  args:
  - name: a
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_cpp_gpu__emit_array_r2
  args:
  - name: a
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_cpp_gpu__emit_array_r3
  args:
  - name: a
    rank: 3
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_cpp_gpu__emit_array_r4
  args:
  - name: a
    rank: 4
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_cpp_gpu__box
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  - name: json
    intent: in
    spec:
      type: string
      len: assumed
  result:
    name: nv
    spec:
      type: derived
      name: harness_cpp_gpu__h_named
- kind: subroutine
  name: harness_cpp_gpu__write_snapshot
  args:
  - name: case_id
    intent: in
    spec:
      type: string
      len: assumed
  - name: values
    rank: 1
    intent: in
    spec:
      type: derived
      name: harness_cpp_gpu__h_named
  - name: time
    intent: in
    spec:
      type: real
      kind: dp
- kind: subroutine
  name: harness_cpp_gpu__write_metrics_basis
  args:
  - name: entries
    rank: 1
    intent: in
    spec:
      type: derived
      name: harness_cpp_gpu__h_mb_entry
  - name: n
    intent: in
    spec:
      type: integer
- kind: subroutine
  name: harness_cpp_gpu__write_diagnostics
  args:
  - name: results
    rank: 1
    intent: in
    spec:
      type: derived
      name: harness_cpp_gpu__h_case_result
  - name: n
    intent: in
    spec:
      type: integer
- kind: subroutine
  name: harness_cpp_gpu__write_perf
  args:
  - name: case_id
    intent: in
    spec:
      type: string
      len: assumed
  - name: target
    intent: in
    spec:
      type: string
      len: assumed
  - name: steps
    intent: in
    spec:
      type: integer
  - name: cells_updated
    intent: in
    spec:
      type: integer
  - name: walltime_sec
    intent: in
    spec:
      type: real
      kind: dp
  - name: mpi_ranks
    intent: in
    spec:
      type: integer
  - name: threads_per_rank
    intent: in
    spec:
      type: integer
  - name: gpu_devices
    intent: in
    spec:
      type: integer
"""

_PIN_DRIFT_HINT = (
    "the certified harness interface no longer matches the renderer's pinned expectation — a "
    "harness recert changed its published surface; update the renderer pin "
    "(_HARNESS_V1_INTERFACE) AND the render template together, then re-certify dependent nodes")


def _pinned_stanzas() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """The embedded §5.1 block lowered to C++ and read back: `(procedure stanzas, type
    stanzas)`, keyed by symbol. A block that does not load is a renderer bug."""
    struct, error = cpp_signatures.load_structured_signatures(_HARNESS_V1_INTERFACE)
    if error:
        raise RenderError(f"internal: the embedded harness interface does not load: {error}")
    ops: dict[str, list[str]] = {}
    types: dict[str, list[str]] = {}
    for sig in [*struct.get("types", []), *struct.get("procedures", [])]:
        o, t, _i, errs = cpp_signatures.parse_interface_stanzas(cpp_signatures.render_symbol(sig))
        if errs:
            raise RenderError(f"internal: the embedded harness interface does not lower: {errs}")
        ops.update(o)
        types.update(t)
    return ops, types


def assert_harness_pin(
    ir: dict[str, Any],
    spec_id: str,
    harness_spec_id: str,
    harness_signatures: Any,
    harness_source: str,
) -> None:
    """Fail-closed guard the conductor runs BEFORE rendering: the certified harness the consumer
    will link against must still publish exactly the declarations this renderer was written for.
    Any drift raises ``RenderError`` (transport fail_closed with the recert-drift hint), never a
    Generate content retry.

    ``harness_signatures`` is the certified harness IR's ``public_api.signatures`` (a list of
    ``{symbol, signature}``); ``harness_source`` is the text of the certified
    ``<harness_spec_id>_model.cu``. For each harness symbol the rendered glue uses:

    1. its IR signature, lowered to C++, must equal the pinned one — a type's members IN ORDER,
       a procedure's canonical lines as a set (its header line pins the argument order);
    2. a PROCEDURE's definition in namespace ``<harness_spec_id>_model`` of the model source must
       carry the same canonical lines. A TYPE is not compared against the source: the model
       source defines none — the host rendered the type definitions into the harness's header
       from the same IR signatures step 1 compares, and a model source that defined one again
       would have failed the harness's own §5.1 pin.

    The two §5.1 module parameters are not stanzas. Their VALUES are pinned where they matter,
    in the compiled runner: it `static_assert`s the harness header's `dp` is `double` and its
    `case_id_len` is `CASE_ID_LEN` (`render_runner`), so a drift of either is a compile error
    rather than a silent narrowing.

    Signatures that resolve to nothing usable (None / empty / malformed) fail closed as a
    missing-artifact error, distinctly from interface drift."""
    if (harness_spec_id or "").strip() != EXPECTED_HARNESS_SPEC_ID:
        raise RenderError(
            f"harness_spec_id {harness_spec_id!r} is not the pinned "
            f"{EXPECTED_HARNESS_SPEC_ID!r}; the renderer only targets that harness")
    exp_ops, exp_types = _pinned_stanzas()

    ir_stanzas: dict[str, list[str]] = {}
    for entry in (harness_signatures if isinstance(harness_signatures, list) else []):
        if not (isinstance(entry, dict) and isinstance(entry.get("symbol"), str)
                and entry["symbol"].strip() and isinstance(entry.get("signature"), dict)
                and entry["signature"]):
            continue
        try:
            o, t, _i, errs = cpp_signatures.parse_interface_stanzas(
                cpp_signatures.render_symbol(entry["signature"]))
        except cpp_signatures.SignatureParseError:
            continue
        if errs:
            continue
        stanza = o.get(entry["symbol"].strip()) or t.get(entry["symbol"].strip())
        if stanza is not None:
            ir_stanzas[entry["symbol"].strip()] = stanza
    if not ir_stanzas:
        raise RenderError(
            f"certified harness IR carries no usable public_api.signatures "
            f"(got {type(harness_signatures).__name__}) — a missing/incomplete certified-IR "
            "artifact (or a caller that failed to resolve it), NOT interface drift; re-certify "
            "the harness IR (run_workflow.py --with-deps) so its public_api.signatures is present")

    src_ops, _src_types, _src_ifaces, _src_errs = cpp_signatures._stanzas(
        cpp_decls.read(harness_source or ""), (f"{harness_spec_id}_model",))

    used = [_hname(harness_spec_id, op) for op in _used_harness_ops(ir)]
    used += [_hname(harness_spec_id, t) for t in _HARNESS_TYPES]
    for symbol in used:
        is_type = symbol in exp_types
        expected = exp_types.get(symbol) if is_type else exp_ops.get(symbol)
        if expected is None:  # a renderer bug: the template depends on an un-embedded symbol
            raise RenderError(
                f"internal: renderer depends on harness symbol {symbol!r} not present in the "
                "embedded pinned interface")
        have = ir_stanzas.get(symbol)
        if have is None:
            raise RenderError(
                f"certified harness IR public_api.signatures omits {symbol!r}: {_PIN_DRIFT_HINT}")
        same = (cpp_signatures.stanza_line_list(have) == cpp_signatures.stanza_line_list(expected)
                if is_type else
                cpp_signatures.stanza_line_set(have) == cpp_signatures.stanza_line_set(expected))
        if not same:
            raise RenderError(
                f"certified harness IR signature for {symbol!r} differs from the pinned "
                f"interface: {_PIN_DRIFT_HINT}")
        if is_type:
            continue
        defined = src_ops.get(symbol)
        if defined is None:
            raise RenderError(
                f"certified harness model source omits {symbol!r}: {_PIN_DRIFT_HINT}")
        if cpp_signatures.stanza_line_set(defined) != cpp_signatures.stanza_line_set(expected):
            raise RenderError(
                f"certified harness model source signature for {symbol!r} differs from the "
                f"pinned interface: {_PIN_DRIFT_HINT}")


# --- the leaf-facing remedies of the checks ABI -------------------------------------------------
#
# The neutral gates decide WHETHER the checks source defines the ABI and the bound state
# (`codegen_bundle.m3c_checks_abi_violation`, `_m3c_state_binding_mismatch`, the validator's
# `_validate_checks_source_files`); HOW the leaf is told to fix it names this language's
# declarations, so the sentences are this backend's.


def _declared_abi() -> str:
    return "; ".join(_abi_declaration(name) for name in CHECKS_PUBLIC_NAMES)


def checks_abi_publication_violation(spec_id: str, unpublished: list[str],
                                     wrong_kind: list[str]) -> str:
    """The acceptance layer's refusal of a checks source that does not define an ABI callback
    with the declared signature (`unpublished`). `wrong_kind` is always empty for this language
    (`source.checks_module_abi_facts` reports a mismatched definition as not defined), and is
    stated anyway if a caller passes one."""
    parts = []
    if unpublished:
        parts.append(
            f"not defined in namespace {spec_id}_checks with the declared parameter types: "
            f"{', '.join(unpublished)}")
    if wrong_kind:
        parts.append(f"defined with another return type: {', '.join(wrong_kind)}")
    return (f"{bundle.checks_basename(spec_id)} must DEFINE the fixed checks ABI the "
            f"host-rendered {checks_abi.checks_header_basename(spec_id)} declares — " + "; ".join(parts)
            + f". Include that header and define each callback in namespace {spec_id}_checks, "
            f"with external linkage (not `static`, not in an unnamed namespace) and exactly the "
            f"declared parameter types (the parameter names are free): {_declared_abi()} The "
            f"full set is required for EVERY physics node, whatever subset this node's runner "
            f"calls (a node with no metrics still defines metric_compute).")


def bound_state_publication_violation(spec_id: str, hidden: list[str]) -> str:
    """The acceptance layer's refusal of a checks source that does not define bound state."""
    return (f"{bundle.checks_basename(spec_id)} must DEFINE every bound state variable — the "
            f"host-rendered runner reads each one as `{spec_id}_checks::<var>` and serializes it "
            f"at the two capture points, and {checks_abi.checks_header_basename(spec_id)} declares it "
            f"`extern` — but these have no definition in namespace {spec_id}_checks: "
            f"{', '.join(hidden)}. Define each at namespace scope with the declared type "
            f"(`double` for a scalar, `std::vector<double>` for rank 1, "
            f"`atmofab::Array<double, R>` for rank R), with external linkage, and size an array "
            f"in `case_setup`.")


def hidden_bound_state_remedy(hidden: list[str]) -> str:
    """The `Generate.gate` static check's statement of the same defect, after the file path."""
    return (f"checks source must define every bound state variable at namespace scope of the "
            f"checks namespace, with external linkage (the host-rendered runner reads each IR "
            f"snapshot variable as `<spec_id>_checks::<var>` and the host-rendered checks header "
            f"declares it `extern`); not defined: {hidden}")


def state_binding_module_reason(module: str) -> str:
    """Why a binding's `module` must be the checks namespace: how the runner reads the storage."""
    return f"the host-rendered runner reads the bound storage as `{module}::<var>`"


def state_binding_storage_reason(variable: str) -> str:
    """Why a binding's `storage_symbol` must equal its variable: the runner's reference to it."""
    return (f"the runner reads the namespace-scope variable of THAT name "
            f"(`<spec_id>_checks::{variable}`)")
