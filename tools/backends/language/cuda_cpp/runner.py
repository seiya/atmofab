"""The CUDA C++ backend's `runner_render` capability: a physics node's host-authored runner glue
(issue #289, R4-b PR-6).

Pure-function module reached through `registry.capability_module("language", "cuda_cpp",
"runner_render")` — the neutral seam is `tools/host_render.py`, which is what the conductor and
the compile gate call. Nothing outside this package imports it by name.

It renders ``<spec_id>_runner.cu`` of a `component` / `problem` node from the node's IR
(`render_runner`): the deterministic driver (issue #589). Per case it reads the host-written case
file through the certified ``harness_cpp_gpu`` plumbing, allocates the state, calls the operations
the node's model publishes, owns the time loop on a `loop` node, and emits the standard runner
outputs through that plumbing, which owns the JSON envelope assembly (harness controlled_spec §3 /
§5.1). The runner holds no serialization knowledge: it calls the writers — it never formats a JSON
token, and it judges nothing.

Split of authorship on a physics node:
- ``<spec_id>_model.cu`` — the physics kernel + the published operations   (LLM leaf)
- ``<spec_id>_runner.cu`` — this renderer                                  (host)
- ``<spec_id>_model.cuh`` — the published-surface header (`header.py`)     (host)
- ``src/Makefile`` — the build system's control-file renderer              (host)

Every snapshot variable is a namespace-scope variable of the driver, ``st_<var>``: a scalar is a
``double``, a rank-1 array a ``std::vector<double>``, a rank-R array an ``atmofab::Array<double, R>``
(the owning column-major array the published-surface header defines); the driver hands the
operations and the emitters a non-owning ``atmofab::View`` over it. So no generated function can
filter, compute or rewrite a captured value on its way to the emitters, and a state's type is never
declared across two translation units.

The language-neutral IR readers — the snapshot schema, the cases, and their refusals — are `tools/runner_ir.py`'s, shared with every backend that renders a runner.
This module adds the refusals CUDA C++ imposes on a driver variable's name (`_check_snapshot_name`). It
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
from tools.backends.language.cuda_cpp import bundle
from tools.backends.language.cuda_cpp import declarations as cpp_decls
from tools.backends.language.cuda_cpp import signatures as cpp_signatures
from tools.host_execution import perf_parallelism

# `RenderError` is the seam's class, not this module's: importing it is what makes the neutral
# `except RenderError:` clause in the conductor work (see `tools/host_render.py`).
from tools.host_render import RenderError

#: The harness's `case_id_len` — the neutral reader's bound on a declared case id, restated as
#: the value the runner pins the certified harness header to (`static_assert`, see
#: `render_runner`).
CASE_ID_LEN = runner_ir.CASE_ID_LEN

#: The bound on a spec_id is the language's (`bundle.SPEC_ID_MAX`); the render-time check below is
#: the backstop to the neutral spec-input gate.
MAX_SPEC_ID_LEN = bundle.SPEC_ID_MAX

_IDENTIFIER_RE = re.compile(bundle.IDENTIFIER_PATTERN)

#: The harness symbols this template calls, pinned by `assert_harness_pin` against the certified
#: harness IR and source. Emitters are added per rank on demand (`_used_harness_ops`).
_HARNESS_TYPES = (
    "h_named",
)
_HARNESS_CORE_OPS = (
    "parse_cases", "box", "write_snapshot", "write_perf", "block_size", "read_case",
    "get_array_r4",
)

#: The target a DRY render is given: `ir_content_violations` renders only to learn whether the
#: IR renders, and discards the text. Both values reach nothing but the perf-record line, which
#: raises for no value, so they decide no violation.
_DRY_RUN_TARGET: dict[str, Any] = {"hardware": {"class": "gpu"},
                                   "execution": {"threads_per_rank": 1}}


# --- the IR, read through the neutral readers ---------------------------------------------------


def _check_snapshot_name(name: str) -> None:
    """The CUDA C++ rules on a snapshot variable's name: it IS the driver's state variable
    `st_<name>`, so it must be an identifier of the bundle's grammar, and not a keyword (a snapshot
    variable must be declarable in every target language)."""
    if not _IDENTIFIER_RE.fullmatch(name):
        raise RenderError(
            f"snapshot variable {name!r} is not a bindable identifier: every snapshot variable "
            "is the driver's state variable of that name, so it must match "
            f"{bundle.IDENTIFIER_PATTERN}")
    if name in cpp_signatures.CPP_KEYWORDS:
        raise RenderError(
            f"snapshot variable {name!r} is a C++ keyword — rename it (a snapshot variable must "
            "be declarable in every target language)")


def _snapshot_schema(ir: dict[str, Any]) -> tuple[dict[str, str], str]:
    return runner_ir.snapshot_schema(ir, _check_snapshot_name)


def _state_type(rank: int) -> str:
    """The C++ type of a state variable of the driver, of `rank`."""
    if rank == 0:
        return "double"
    if rank == 1:
        return "std::vector<double>"
    return f"{cpp_signatures.ARRAY_TYPE}<double, {rank}>"


def _clit(value: str) -> str:
    """`value` as the contents of a C++ string literal.

    IR-sourced names (case ids, snapshot variable names, test ids) are only
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
    """Unqualified harness op names the rendered driver calls (deterministic order): the core
    writers and readers, the emitters and getters of what the IR declares, and the rank-1 emitter
    of the series on a `loop` node. `assert_harness_pin` compares exactly these, so a name left
    out here is an unenforced pin."""
    schema, _ = _snapshot_schema(ir)
    plan = runner_ir.driver_plan(ir, schema)
    ops = ["parse_cases", "read_case", *plan.getters, "emit_real"]
    ranks = set(plan.array_ranks) | ({1} if plan.loop else set())
    ops += [f"emit_array_r{r}" for r in sorted(ranks)]
    ops += ["box", "write_snapshot", "write_perf", "block_size"]
    return ops


def _hname(harness_spec_id: str, sym: str) -> str:
    return f"{harness_spec_id}__{sym}"


def _check_identifier_lengths(spec_id: str, harness_spec_id: str) -> None:
    # Node-IDENTITY defects (a re-author cannot shorten the spec_id / harness id), so
    # `identity=True`: the compile.static mirror excludes them (spec-input concern).
    if len(spec_id) > MAX_SPEC_ID_LEN:
        raise RenderError(
            f"spec_id {spec_id!r} is {len(spec_id)} chars (>{MAX_SPEC_ID_LEN}); the derived "
            f"`<spec_id>_runner` identifier would exceed the "
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


def render_runner(ir: dict[str, Any], spec_id: str, harness_spec_id: str,
                  *, target: dict[str, Any]) -> str:
    """Render ``<spec_id>_runner.cu``, the driver, from the IR and the target. Deterministic and
    pure.

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
    runner_ir.case_ids(ir)   # the refusals of a case id (the runner takes the ids from argv)
    target_class = runner_ir.target_class(target)
    ranks, threads, devices = perf_parallelism(target)
    plan = runner_ir.driver_plan(ir, schema_vars)

    hm = f"{harness_spec_id}_model"
    md = f"{spec_id}_model"

    def H(sym: str) -> str:  # local shorthand, as in the Fortran renderer
        return f"hm::{_hname(harness_spec_id, sym)}"

    lines: list[str] = []
    a = lines.append
    a(f"// {bundle.runner_basename(spec_id)}: rendered by the host from the node's IR. Do not "
      "edit.")
    a("// The driver (issue #589): per case it reads the host-written case file, allocates the")
    a("// state, calls the published operations, owns the time loop on a loop node, and writes")
    a(f"// every capture THROUGH the certified {harness_spec_id} plumbing, which owns all JSON")
    a("// assembly. The state is host memory; an operation copies to and from the device itself.")
    a(f'#include "{hm}.cuh"')
    a(f'#include "{md}.cuh"')
    a("")
    for header in ("algorithm", "chrono", "cmath", "cstddef", "cstdint", "cstdio", "cstdlib",
                   "iostream", "limits", "string", "type_traits", "vector"):
        a(f"#include <{header}>")
    a("")
    a("namespace {")
    a("")
    a(f"namespace hm = {hm};")
    a(f"namespace md = {md};")
    a("using dp = double;")
    a("")
    # The two §5.1 module parameters whose VALUES this glue relies on: the driver holds float64
    # state the harness takes as `dp`, and a declared case id is bounded by the harness's
    # `case_id_len` (`runner_ir.CASE_ID_LEN`). The signature pin compares prototypes, which spell
    # `dp` whatever it is; these make a drift of either value a compile error.
    a("static_assert(std::is_same<hm::dp, double>::value,")
    a(f'              "the certified {harness_spec_id} dp is not the float64 this driver holds");')
    a(f"static_assert(hm::case_id_len == {CASE_ID_LEN},")
    a(f'              "the certified {harness_spec_id} case_id_len is not the width this runner '
      'bounds case ids by");')
    a("")
    a(f"using Named = hm::{_hname(harness_spec_id, 'h_named')};")
    a("")
    # Every exit of the program, the successful one included, goes through `finish`, which
    # flushes and ends the process with `std::_Exit`: no namespace-scope destructor and no exit
    # handler of the leaf's sources runs after the harness has written the run's outputs (round
    # 3 of this change's review — a C++ program otherwise runs them after `main` returns, and a
    # model source's destructor rewrote a run output there with every gate green).
    # `std::_Exit` also skips the exit hook through which a CUDA profiler flushes its kernel
    # records, so a trace of the run held no kernel and the device-kernel gate refused a node
    # whose kernels ran (issue #307, measured at a gpu site with the profiler the trace uses).
    # `cudaDeviceReset` flushes them first; it runs no code of the node's sources.
    a("// Every exit ends here: flush, then end the process with no destructor or exit handler of")
    a("// the node's sources running after the harness has written the run's outputs. The device")
    a("// reset first hands a profiler tracing the run the kernel records it would otherwise lose.")
    a("[[noreturn]] void finish(int code) {")
    a("  std::cout.flush();")
    a("  std::cerr.flush();")
    a("  std::fflush(nullptr);")
    a("  (void)cudaDeviceReset();")
    a("  std::_Exit(code);")
    a("}")
    a("")
    a("[[noreturn]] void refuse(const std::string& what, const std::string& cid) {")
    a('  std::cerr << "error: " << what << " for case " << cid << std::endl;')
    a("  finish(2);")
    a("}")
    a("")
    _render_views(a, plan)
    _render_storage(a, plan)
    a("int step_c = 0;")
    a("double t_c = 0.0;")
    a("")
    for proc in (p for entry in plan.entries for p in entry.procedures):
        _render_procedure(a, plan, proc)
    a("// Every declared state variable, `t` (the writer's) and `step`: the same set at every")
    a("// capture point of every case.")
    a("std::vector<Named> capture_state() {")
    a("  std::vector<Named> out;")
    for var, rank in plan.variables.items():
        vlit = _clit(var)
        if rank == 0:
            a(f'  out.push_back({H("box")}("{vlit}", {H("emit_real")}(st_{var})));')
        else:
            a(f'  out.push_back({H("box")}("{vlit}", {H(f"emit_array_r{rank}")}(cv(st_{var}))));')
    a(f'  out.push_back({H("box")}("step", {H("emit_real")}(static_cast<double>(step_c))));')
    a("  return out;")
    a("}")
    a("")
    a("void write_capture(const std::string& name) {")
    a(f"  {H('write_snapshot')}(name, capture_state(), t_c);")
    a("}")
    a("")
    if plan.loop:
        a("// A committed array's extremum, NaN when any cell is non-finite: a committed non-finite")
        a("// cell must reach the series, whose loader refuses it.")
        a("template <class A>")
        a("double fold(const A& state, bool want_max) {")
        a("  double x = want_max ? -std::numeric_limits<double>::infinity()")
        a("                      : std::numeric_limits<double>::infinity();")
        a("  const std::size_t n = flat_size(state);")
        a("  const double* p = flat_data(state);")
        a("  for (std::size_t k = 0; k < n; ++k) {")
        a("    if (!std::isfinite(p[k])) {")
        a("      return std::numeric_limits<double>::quiet_NaN();")
        a("    }")
        a("    x = want_max ? std::max(x, p[k]) : std::min(x, p[k]);")
        a("  }")
        a("  return x;")
        a("}")
        a("")
        a("// One series column; the empty list is this glue's own token, since no emitter contract")
        a("// covers a zero-length array.")
        a("std::string series_json(std::vector<double>& column) {")
        a("  if (column.empty()) {")
        a('    return "[]";')
        a("  }")
        a(f"  return {H('emit_array_r1')}(cv(column));")
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
    # The block size the host imposed on the run (issue #512, harness §3.3), handed to the
    # node's sources through the rendered header before any of their code runs: their kernels
    # launch with it, and never name the harness.
    a("  // --- the threads per block the host imposed on this run's kernels (harness §3.3) ----")
    a(f"  atmofab::launch_block_size = {H('block_size')}();")
    a("")
    a("  int steps_total = 0;")
    a("  std::int64_t cells_total = 0;")
    a("  double walltime = 0.0;")
    a("")
    a("  for (int ci = 0; ci < ncases; ++ci) {")
    a("    const std::string cid = case_ids[static_cast<std::size_t>(ci)];")
    a(f"    {H('read_case')}(cid, ok);")
    a("    if (!ok) {")
    a('      refuse("no readable case file", cid);')
    a("    }")
    a("    step_c = 0;")
    a("    t_c = 0.0;")
    a("    bool case_refused = false;")
    if plan.loop:
        a("    int n_c = 0;")
        a("    int every_c = 0;")
        a("    double dt_c = 0.0;")
        a("    double t_start_c = 0.0;")
        a(f"    std::vector<std::vector<double>> se({len(plan.series_names)});")
    if plan.list_form:
        for k, entry in enumerate(plan.entries, start=1):
            joined = " || ".join(f'cid == "{_clit(c)}"' for c in entry.cases or ())
            a(f"    {'if' if k == 1 else '} else if'} ({joined}) {{")
            _render_entry(a, plan, entry, H, "      ")
        a("    } else {")
        a('      refuse("no drive entry runs this case", cid);')
        a("    }")
    else:
        _render_entry(a, plan, plan.entries[0], H, "    ")
    a("    steps_total += step_c;")
    sizes = [f"flat_size(st_{v})" for v in plan.arrays]
    largest = "std::size_t{1}"
    for size in sizes:
        largest = f"std::max({largest}, {size})"
    a(f"    cells_total += static_cast<std::int64_t>(step_c) * "
      f"static_cast<std::int64_t>({largest});")
    a("  }")
    a("")
    a("  if (walltime <= 0.0) {")
    a("    walltime = 1.0e-9;")
    a("  }")
    a("  // --- emit the perf record (harness owns its envelope) --------------------------")
    a(f'  {H("write_perf")}(case_ids[static_cast<std::size_t>(ncases - 1)], '
      f'"{_clit(target_class)}", steps_total,')
    a("      static_cast<int>(std::min<std::int64_t>(cells_total, "
      "std::numeric_limits<int>::max())),")
    a(f"      walltime, {ranks}, {threads}, {devices});")
    a("  finish(0);")
    a("}")
    return "\n".join(lines) + "\n"


def _render_views(a: Any, plan: runner_ir.DriverPlan) -> None:
    """The small overloads the driver reaches its storage through."""
    a("// A non-owning view of a state array: writable (`mv`) or read-only (`cv`).")
    a("inline atmofab::View<double, 1> mv(std::vector<double>& s) {")
    a("  return {s.data(), {static_cast<long>(s.size())}};")
    a("}")
    a("inline atmofab::View<const double, 1> cv(const std::vector<double>& s) {")
    a("  return {s.data(), {static_cast<long>(s.size())}};")
    a("}")
    a("inline std::size_t flat_size(const std::vector<double>& s) {")
    a("  return s.size();")
    a("}")
    a("inline const double* flat_data(const std::vector<double>& s) {")
    a("  return s.data();")
    a("}")
    a("inline void fill(std::vector<double>& s, double x) {")
    a("  std::fill(s.begin(), s.end(), x);")
    a("}")
    host_ranks = {v.rank for v in plan.hosts if v.rank}
    for rank in sorted((set(plan.array_ranks) | host_ranks) - {1}):
        idx = ", ".join(f"s.extent[{k}]" for k in range(rank))
        a(f"inline atmofab::View<double, {rank}> mv(atmofab::Array<double, {rank}>& s) {{")
        a(f"  return {{s.data.data(), {{{idx}}}}};")
        a("}")
        a(f"inline atmofab::View<const double, {rank}> cv(const atmofab::Array<double, {rank}>& s)"
          " {")
        a(f"  return {{s.data.data(), {{{idx}}}}};")
        a("}")
        a(f"inline std::size_t flat_size(const atmofab::Array<double, {rank}>& s) {{")
        a("  return s.data.size();")
        a("}")
        a(f"inline const double* flat_data(const atmofab::Array<double, {rank}>& s) {{")
        a("  return s.data.data();")
        a("}")
        a(f"inline void fill(atmofab::Array<double, {rank}>& s, double x) {{")
        a("  std::fill(s.data.begin(), s.data.end(), x);")
        a("}")
    a("")


def _render_storage(a: Any, plan: runner_ir.DriverPlan) -> None:
    for var, rank in plan.variables.items():
        a(f"{_state_type(rank)} st_{var}{{}};")
    for var in plan.work_vars:
        a(f"{_state_type(plan.variables[var])} wk_{var}{{}};")
    for var in plan.logical_states:
        a(f"bool lg_{var} = false;")
    for var in plan.integer_states:
        a(f"int iv_{var} = 0;")
    for value in plan.hosts:
        cpp = (_state_type(value.rank) if value.rank else
               {"real": "double", "integer": "int", "logical": "bool"}[value.kind])
        a(f"{cpp} {value.local}{{}};")


def _real_literal(value: float) -> str:
    text = format(float(value), ".17g")
    return text if any(ch in text for ch in ".e") else f"{text}.0"


def _render_procedure(a: Any, plan: runner_ir.DriverPlan, proc: runner_ir.ProcedurePlan) -> None:
    """A host-rendered procedure of the prototype (D11): the elementwise assignment of its one
    array `out`, then the driver's call records. Rendered with the prototype's own lowering, so
    the operation's `{type: procedure}` argument takes it."""
    args = {x["name"]: x for x in proc.prototype.get("args") or []}

    def name(token: str) -> str:
        if token in proc.hosts:
            return proc.hosts[token]
        if token not in args:
            raise RenderError(f"procedure expression names {token!r}, not a dummy of its "
                              "prototype")
        return f"{token}.data[p]" if runner_ir._dummy_rank(args[token]) else token

    def function(fn: str, xs: list[str]) -> str:
        if fn in ("min", "max"):
            out = xs[0]
            for x in xs[1:]:
                out = f"std::{fn}<double>({out}, {x})"
            return out
        return f"std::{'fabs' if fn == 'abs' else fn}({', '.join(xs)})"

    expr = runner_ir.lower_expression(
        proc.tree, name=name, number=_real_literal,
        power=lambda x, y: f"std::pow({x}, {y})", function=function)
    params = cpp_signatures._render_params(proc.prototype.get("args") or [], proc.name)
    a(f"void {proc.name}(")
    for i, param in enumerate(params):
        a(f"    {param}{',' if i < len(params) - 1 else ') {'}")
    out = proc.out
    rank = runner_ir._dummy_rank(args[out])
    size = " * ".join(f"static_cast<std::size_t>({out}.extent[{k}])" for k in range(rank))
    a(f"  const std::size_t n = {size};")
    a("  for (std::size_t p = 0; p < n; ++p) {")
    a(f"    {out}.data[p] = {expr};")
    a("  }")
    for param_name, spec in args.items():
        used = (param_name == out or any(d == param_name for d, _ in proc.record)
                or param_name in _names_of(proc.tree))
        if not used:
            a(f"  (void){param_name};")
        del spec
    if proc.calls:
        a(f"  st_{proc.calls} += 1.0;")
    if proc.record:
        a(f"  const int call_no = {f'static_cast<int>(st_{proc.calls})' if proc.calls else '1'};")
    for dummy, refs in proc.record:
        if runner_ir._dummy_rank(args[dummy]) == 0:
            sv = f"st_{refs[0]}"
            a(f"  if (static_cast<std::size_t>(call_no) <= {sv}.size()) {{")
            a(f"    {sv}[static_cast<std::size_t>(call_no - 1)] = {dummy};")
            a("  }")
            continue
        for k, ref in enumerate(refs, start=1):
            a(f"  if (call_no == {k}) {{")
            data = f"st_{ref}.data" if plan.variables[ref] >= 2 else f"st_{ref}"
            a(f"    {data}.assign({dummy}.data, {dummy}.data + n);")
            a("  }")
    a("}")
    a("")


def _names_of(tree: Any) -> set[str]:
    import ast as _ast
    return {n.id for n in _ast.walk(tree) if isinstance(n, _ast.Name)}


def _actual(plan: runner_ir.DriverPlan, actual: runner_ir.Actual) -> str:
    form, ref = actual.form, actual.ref
    if form == "host":
        return f"cv({ref.local})" if ref.rank else ref.local
    if form == "state":
        if plan.variables[ref] == 0:
            return f"st_{ref}"
        return f"{'mv' if actual.writes else 'cv'}(st_{ref})"
    if form == "work":
        return f"mv(wk_{ref})"
    if form == "logical":
        return f"lg_{ref}"
    if form == "integer":
        return f"iv_{ref}"
    if form == "state_int":
        return f"static_cast<int>(std::lround(st_{ref}))"
    if form == "time":
        return "t_c"
    if form == "step":
        return "step_c"
    if form == "literal":
        kind, value = ref
        if kind == "logical":
            return "true" if value else "false"
        return str(int(value)) if kind == "integer" else _real_literal(float(value))
    return ref.name


def _render_call(a: Any, plan: runner_ir.DriverPlan, call: runner_ir.CallPlan, indent: str, *,
                 timed: bool = False) -> None:
    """Every logical `out` false before the call and written as 1 / 0 after it (D1)."""
    for var in call.logical:
        a(f"{indent}lg_{var} = false;")
    for var in call.integer:
        a(f"{indent}iv_{var} = 0;")
    if timed:
        a(f"{indent}const auto clock0 = std::chrono::steady_clock::now();")
    actuals = ", ".join(_actual(plan, x) for x in call.actuals)
    a(f"{indent}md::{call.operation}({actuals});")
    if timed:
        a(f"{indent}walltime += std::chrono::duration<double>(")
        a(f"{indent}    std::chrono::steady_clock::now() - clock0).count();")
    for var in call.logical:
        a(f"{indent}st_{var} = lg_{var} ? 1.0 : 0.0;")
    for var in call.integer:
        a(f"{indent}st_{var} = static_cast<double>(iv_{var});")


def _render_entry(a: Any, plan: runner_ir.DriverPlan, entry: runner_ir.EntryPlan, H: Any,
                  ind: str) -> None:
    a(f"{ind}// --- the case file, read whole before any capture ---")
    for value in entry.hosts:
        key = _clit(value.key)
        if value.rank:
            a(f'{ind}{H(f"get_array_r{value.rank}")}("{key}", {value.local});')
        else:
            a(f'{ind}{value.local} = {H(runner_ir.GETTERS[value.kind])}("{key}");')
    for var in entry.from_file:
        rank = plan.variables[var]
        if rank:
            a(f'{ind}{H(f"get_array_r{rank}")}("{_clit(var)}", st_{var});')
        else:
            a(f'{ind}st_{var} = {H("get_real")}("{_clit(var)}");')
    for var in entry.zero_scalars:
        a(f"{ind}st_{var} = 0.0;")
    for var, extents in entry.zero_arrays.items():
        if plan.variables[var] == 1:
            a(f"{ind}st_{var}.assign(static_cast<std::size_t>({extents[0]}), 0.0);")
            continue
        a(f"{ind}{{")
        size = " * ".join(f"static_cast<std::size_t>({e})" for e in extents)
        a(f"{ind}  st_{var}.data.assign({size}, 0.0);")
        for k, e in enumerate(extents):
            a(f"{ind}  st_{var}.extent[{k}] = {e};")
        a(f"{ind}}}")
    for var in plan.work_vars:
        a(f"{ind}wk_{var} = st_{var};")
    if plan.loop:
        for key, target in (("t_start", "t_start_c"), ("dt", "dt_c")):
            form, ref = entry.loop_values[key]
            a(f"{ind}{target} = {ref.local if form == 'host' else '0.0'};")
        form, ref = entry.loop_values["n_step"]
        a(f"{ind}n_c = "
          f"{'static_cast<int>(std::lround(' + ref.local + '))' if form == 'host' else '0'};")
        a(f"{ind}every_c = static_cast<int>(std::lround({entry.loop_values['every'][1].local}));")
        a(f"{ind}t_c = t_start_c;")
    if entry.init is not None:
        a(f"{ind}// --- init, on the state itself; a false result refuses the case ---")
        _render_call(a, plan, entry.init, ind)
        a(f"{ind}case_refused = !lg_{entry.init.result_state};")
        if plan.loop:
            state_loop = {k: v[1] for k, v in entry.loop_values.items() if v[0] == "state"}
            if state_loop:
                a(f"{ind}if (!case_refused) {{")
                if "t_start" in state_loop:
                    a(f"{ind}  t_start_c = st_{state_loop['t_start']};")
                    a(f"{ind}  t_c = t_start_c;")
                if "dt" in state_loop:
                    a(f"{ind}  dt_c = st_{state_loop['dt']};")
                    a(f"{ind}  if (!(dt_c > 0.0)) {{")
                    a(f'{ind}    refuse("init wrote a dt <= 0", cid);')
                    a(f"{ind}  }}")
                if "n_step" in state_loop:
                    a(f"{ind}  n_c = static_cast<int>(std::lround(st_{state_loop['n_step']}));")
                    a(f"{ind}  if (n_c < 1) {{")
                    a(f'{ind}    refuse("init wrote n_step < 1", cid);')
                    a(f"{ind}  }}")
                a(f"{ind}}}")
            a(f"{ind}if (case_refused) {{")
            a(f"{ind}  n_c = 0;")
            a(f"{ind}}}")
    a(f"{ind}// --- the initial capture (raw/state_snapshots/initial/<case_id>.json) ---")
    a(f'{ind}write_capture("initial/" + cid);')
    if plan.loop:
        a(f"{ind}for (int n = 1; n <= n_c; ++n) {{")
        in1 = ind + "  "
        for var in entry.work_copy:
            a(f"{in1}wk_{var} = st_{var};")
        for var in entry.work_nan:
            a(f"{in1}fill(wk_{var}, std::numeric_limits<double>::quiet_NaN());")
        _render_call(a, plan, entry.step, in1, timed=True)
        a(f"{in1}if (!lg_{entry.step.result_state}) {{")
        a(f"{in1}  break;")
        a(f"{in1}}}")
        a(f"{in1}// --- committed: the state, step, t, the series entry, the sample ---")
        for var in (*entry.work_copy, *entry.work_nan):
            a(f"{in1}st_{var} = wk_{var};")
        a(f"{in1}step_c = n;")
        a(f"{in1}t_c = t_start_c + static_cast<double>(n) * dt_c;")
        a(f"{in1}se[0].push_back(static_cast<double>(n));")
        col = 1
        for var in plan.arrays:
            a(f"{in1}se[{col}].push_back(fold(st_{var}, false));")
            a(f"{in1}se[{col + 1}].push_back(fold(st_{var}, true));")
            col += 2
        a(f"{in1}if (every_c > 0 && n % every_c == 0) {{")
        a(f'{in1}  write_capture("history/" + cid + "__" + std::to_string(n));')
        a(f"{in1}}}")
        a(f"{ind}}}")
    else:
        a(f"{ind}if (!case_refused) {{")
        a(f"{ind}  // --- one call on the state itself: what it leaves is the captured state ---")
        _render_call(a, plan, entry.step, ind + "  ", timed=True)
        a(f"{ind}  if (lg_{entry.step.result_state}) {{")
        a(f"{ind}    step_c = 1;")
        a(f"{ind}  }}")
        a(f"{ind}}}")
    a(f"{ind}// --- the final capture (raw/state_snapshots/<case_id>.json) ---")
    a(f"{ind}write_capture(cid);")
    if plan.loop:
        a(f"{ind}{{")
        a(f"{ind}  std::vector<Named> series;")
        for col, name in enumerate(plan.series_names):
            a(f'{ind}  series.push_back({H("box")}("{_clit(name)}", series_json(se[{col}])));')
        a(f'{ind}  {H("write_snapshot")}("series/" + cid, series, t_c);')
        a(f"{ind}}}")


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
# (spec_version 0.4.0). If a harness recert changes §5.1, THIS block and the render template must
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
- kind: subroutine
  name: harness_cpp_gpu__read_case
  args:
  - name: case_id
    intent: in
    spec:
      type: string
      len: assumed
  - name: ok
    intent: out
    spec:
      type: logical
- kind: function
  name: harness_cpp_gpu__get_real
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  result:
    name: x
    spec:
      type: real
      kind: dp
- kind: function
  name: harness_cpp_gpu__get_int
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  result:
    name: i
    spec:
      type: integer
- kind: function
  name: harness_cpp_gpu__get_bool
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  result:
    name: b
    spec:
      type: logical
- kind: subroutine
  name: harness_cpp_gpu__get_array_r1
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  - name: a
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
      alloc: true
- kind: subroutine
  name: harness_cpp_gpu__get_array_r2
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  - name: a
    rank: 2
    intent: out
    spec:
      type: real
      kind: dp
      alloc: true
- kind: subroutine
  name: harness_cpp_gpu__get_array_r3
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  - name: a
    rank: 3
    intent: out
    spec:
      type: real
      kind: dp
      alloc: true
- kind: subroutine
  name: harness_cpp_gpu__get_array_r4
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  - name: a
    rank: 4
    intent: out
    spec:
      type: real
      kind: dp
      alloc: true
- kind: function
  name: harness_cpp_gpu__block_size
  result:
    name: n
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


def physics_harness_uses(harness_spec_id: str) -> dict[str, frozenset[str]]:
    """None: the one harness this renderer targets runs the program as one process and
    publishes no operation for a physics source (`host_render.physics_harness_uses`)."""
    del harness_spec_id
    return {}
