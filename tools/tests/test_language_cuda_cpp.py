"""The CUDA C++ language backend and its companions (issue #289, R4-b PR-4).

`tools/backends/language/cuda_cpp/` (the masking, the declaration reader, the §5.1 lowering and
source pin, the source gates), `tools/backends/compiler/nvcc`, `tools/backends/linter/nvcc` and
`tools/backends/parallel/cuda`. Rows that launch the real CUDA compiler driver are skipped where
`nvcc` is not on `PATH` (the CI image does not install the CUDA toolkit), and say so.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from tools.backends import registry
from tools.backends.compiler.nvcc import syntax as nvcc_syntax
from tools.backends.language.cuda_cpp import bundle as cpp_bundle
from tools.backends.language.cuda_cpp import checks_abi as cpp_checks_abi
from tools.backends.language.cuda_cpp import declarations as cpp_decls
from tools.backends.language.cuda_cpp import header as cpp_header
from tools.backends.language.cuda_cpp import lines as cpp_lines
from tools.backends.language.cuda_cpp import signatures as cs
from tools.backends.language.cuda_cpp import source as cpp_source
from tools.backends.language.cuda_cpp import syntax as cpp_syntax
from tools.backends.linter.nvcc import lint as nvcc_lint
from tools.backends.parallel.cuda import directives as cuda_directives
from tools.backends.parallel.cuda import execution as cuda_execution
from tools.backends.parallel.cuda import trace as cuda_trace

REPO_ROOT = Path(__file__).resolve().parents[2]
NVCC = shutil.which("nvcc")


def _section51(path: Path) -> str | None:
    m = re.search(r"### 5\.1.*?```ya?ml\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL)
    return m.group(1) if m else None


class MaskTests(unittest.TestCase):
    def test_comments_and_literal_contents_are_blanked_with_offsets_kept(self) -> None:
        text = ('int a; // x { ( ;\n/* multi\nline } */ int b = \'}\';\n'
                'const char* s = "q\\"uote { ;"; auto r = R"d(raw ) " })d"; int c;\n')
        masked = cpp_lines.mask(text)
        self.assertEqual(len(masked), len(text))
        self.assertEqual(masked.count("\n"), text.count("\n"))
        for brace in "{}(":
            self.assertNotIn(brace, masked.replace("R\"d(", "").replace(")d\"", ""))
        self.assertIn("int c;", masked)
        self.assertIn('"', masked)  # delimiters stay

    def test_a_digit_separator_is_not_a_character_literal(self) -> None:
        text = "int n = 1'000'000; int m = 2;\n"
        self.assertEqual(cpp_lines.mask(text), text)

    def test_a_separator_after_an_identifier_and_a_sign_is_not_a_literal(self) -> None:
        """Round 3 of the review: the walk back over a pp-number crossed `+` after an identifier
        ending in `e`, so `e+1'0` opened a character literal that blanked the rest of the line."""
        for text in ("u += e+1'0; int y;\n", "i < size+1'000; ++i\n", "x = 1e+1'0 + e-.5'0;\n"):
            with self.subTest(text=text):
                self.assertEqual(text, cpp_lines.mask(text))
        self.assertEqual("x = u8' ' + L' ';\n", cpp_lines.mask("x = u8'a' + L'b';\n"))

    def test_a_continued_line_comment_continues(self) -> None:
        text = "// a comment \\\nstill { comment\nint x;\n"
        masked = cpp_lines.mask(text)
        self.assertNotIn("{", masked)
        self.assertIn("int x;", masked)

    def test_literals_returns_the_contents_and_line(self) -> None:
        text = 'x = 1;\nprintf("%.16e\\n", v); // "not a literal"\n'
        self.assertEqual([(2, "%.16e\\n")], cpp_lines.literals(text))

    def test_preprocessor_lines_are_blanked_with_their_continuations(self) -> None:
        text = "#define F(x) \\\n  x { \nint y;\n  #  pragma once\n"
        stripped = cpp_lines.strip_preprocessor(cpp_lines.mask(text))
        self.assertNotIn("{", stripped)
        self.assertNotIn("pragma", stripped)
        self.assertIn("int y;", stripped)


_SOURCE = r'''
#pragma once
#include <string>
#include <vector>
namespace atmofab { template <class T, int R> struct View { T* data; long extent[R]; }; }
// namespace commented { void no(); }
namespace demo_model {
using dp = double;
constexpr int case_id_len = 64;
struct demo__h_check {
    std::string id;
    std::string status{"na  "};
    int a, *b;
    void method() const { return; }
  public:
    bool flag = false;
};
using demo__rhs = void (*)(atmofab::View<const dp, 2> u, dp t);
typedef void (*old_rhs)(int a);
[[nodiscard]] inline std::string demo__emit(dp x) { return "{"; }
void demo__parse(const std::vector<std::string> &tokens, int n, bool& ok);
__global__ void kernel(int n, double* y) { if (n) y[0] = 0; }
static void helper(int, double = 1.0);
namespace inner::deeper { void nested(); }
namespace { void hidden() {} }
extern "C" { void c_linkage(int); }
}  // namespace demo_model
void demo_model::demo__parse(const std::vector<std::string> &tokens, int n, bool& ok) {
  ok = true; kernel<<<1, 32>>>(n, nullptr); (void)tokens;
}
int main(int argc, char** argv) { return 0; }
'''


class DeclarationReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decls = cpp_decls.read(_SOURCE)

    def _fn(self, name: str) -> list[cpp_decls.Function]:
        return [f for f in self.decls.functions if f.name == name]

    def test_namespaces_including_nested_anonymous_and_transparent_blocks(self) -> None:
        self.assertEqual([], self.decls.errors)
        self.assertIn(("demo_model",), self.decls.namespaces)
        self.assertIn(("demo_model", "inner", "deeper"), self.decls.namespaces)
        self.assertIn(("demo_model", ""), self.decls.namespaces)
        self.assertEqual(("demo_model", "inner", "deeper"), self._fn("nested")[0].namespace)
        self.assertEqual(("demo_model", ""), self._fn("hidden")[0].namespace)
        self.assertEqual(("demo_model",), self._fn("c_linkage")[0].namespace)
        self.assertNotIn("no", {f.name for f in self.decls.functions})

    def test_a_definition_carries_its_masked_body_and_a_declaration_none(self) -> None:
        """The `problem` model gates read a definition's body (R4-b PR-6): the text between its
        braces, comments and literal contents blanked, directive lines blanked."""
        decls = cpp_decls.read('void f(double& a) {\n  a = 1.0; // x = "{"\n}\n'
                               "void g(int);\n")
        f = next(fn for fn in decls.functions if fn.name == "f")
        g = next(fn for fn in decls.functions if fn.name == "g")
        self.assertIn("a = 1.0;", f.body)
        self.assertNotIn("x =", f.body)
        self.assertEqual("", g.body)

    def test_a_pointer_keeps_its_element_const_whatever_follows_the_star(self) -> None:
        params = cpp_decls.read("void k(const double* __restrict__ u, double* const p, "
                                "const double* const q, const double x) {}").functions[0].params
        self.assertEqual((("const double*__restrict__", "u"), ("double*", "p"),
                          ("const double*", "q"), ("double", "x")), params)

    def test_a_brace_initialized_variable_is_read(self) -> None:
        decls = cpp_decls.read("namespace n {\nstd::vector<double> u{};\n"
                               "inline constexpr int k{64};\ndouble a[2]{{1, 2}};\n}\n")
        self.assertEqual(["u", "k", "a"], [v.name for v in decls.variables])

    def test_a_qualified_definition_joins_its_namespace(self) -> None:
        parse = self._fn("demo__parse")
        self.assertEqual([False, True], [f.defined for f in parse])
        self.assertEqual({("demo_model",)}, {f.namespace for f in parse})
        self.assertEqual(
            (("const std::vector<std::string>&", "tokens"), ("int", "n"), ("bool&", "ok")),
            parse[1].params)

    def test_linkage_specifiers_leave_the_return_type_and_others_stay(self) -> None:
        self.assertEqual("std::string", self._fn("demo__emit")[0].returns)
        self.assertEqual("inline std::string", self._fn("demo__emit")[0].head)
        self.assertEqual("__global__ void", self._fn("kernel")[0].returns)
        self.assertEqual((("int", ""), ("double", "")), self._fn("helper")[0].params)
        self.assertEqual("void", self._fn("helper")[0].returns)
        self.assertEqual("static void", self._fn("helper")[0].head)

    def test_a_type_word_is_never_a_parameter_name(self) -> None:
        self.assertEqual(("unsigned int", ""), cpp_decls.parse_param("unsigned int"))
        self.assertEqual(("const double", ""), cpp_decls.parse_param("const double"))
        self.assertEqual(("unsigned int", "n"), cpp_decls.parse_param("unsigned int n"))

    def test_a_template_and_an_initialized_variable_are_not_functions(self) -> None:
        decls = cpp_decls.read("namespace m {\ntemplate <class T> T twice(T x) { return x + x; }\n"
                               "S s = S(1), t{2};\nint v = f(3);\n}\n")
        self.assertEqual([], decls.errors)
        self.assertEqual([], [f.name for f in decls.functions])

    def test_the_statement_after_a_function_template_is_read(self) -> None:
        decls = cpp_decls.read("namespace m {\ntemplate <class T> T twice(T x) { return x; }\n"
                               "void after(int a);\n"
                               "template <class T> struct Box { T v; };\nvoid last();\n}\n")
        self.assertEqual(["after", "last"], [f.name for f in decls.functions])
        braced = cpp_decls.read("namespace m {\nconstexpr int table[] = {1, 2};\nvoid g();\n}\n")
        self.assertEqual(["table"], [v.name for v in braced.variables])
        self.assertEqual(["g"], [f.name for f in braced.functions])

    def test_struct_data_members_in_order(self) -> None:
        (st,) = [s for s in self.decls.structs if s.name == "demo__h_check"]
        self.assertEqual(
            (("std::string", "id"), ("std::string", "status"), ("int", "a"), ("int*", "b"),
             ("bool", "flag")),
            st.members)

    def test_aliases_variables_and_skipped_templates(self) -> None:
        aliases = {a.name: a.target for a in self.decls.aliases}
        self.assertEqual("double", aliases["dp"])
        self.assertEqual("void(*)(atmofab::View<const dp,2>u,dp t)", aliases["demo__rhs"])
        self.assertEqual("void(*)(int a)", aliases["old_rhs"])
        self.assertEqual(["case_id_len"], [v.name for v in self.decls.variables])
        self.assertNotIn("View", {s.name for s in self.decls.structs})

    def test_an_unbalanced_bracket_is_an_error(self) -> None:
        self.assertTrue(cpp_decls.read("namespace a { void f(;\n").errors)
        self.assertTrue(cpp_decls.read("namespace a { void f() {}\n").errors)


def _struct(procedures=(), types=(), module_parameters=(), interfaces=()) -> dict:
    return {"module_parameters": list(module_parameters), "types": list(types),
            "interfaces": list(interfaces), "procedures": list(procedures)}


def _arg(arg_name, type_, rank=0, intent="in", **spec) -> dict:
    ent = {"name": arg_name, "spec": {"type": type_, **spec}}
    if rank:
        ent["rank"] = rank
    if intent is not None:
        ent["intent"] = intent
    return ent


class LoweringTests(unittest.TestCase):
    """One row per line of the lowering table (`signatures` module docstring)."""

    def _param(self, arg: dict) -> str:
        text = cs.render_symbol({"kind": "subroutine", "name": "d__f", "args": [arg]})
        return text.splitlines()[1].strip().rstrip(");,")

    def test_each_argument_row(self) -> None:
        rows = [
            (_arg("x", "real", kind="dp"), "dp x"),
            (_arg("x", "real", kind="dp", intent="out"), "dp& x"),
            (_arg("n", "integer"), "int n"),
            (_arg("n", "integer", intent="inout"), "int& n"),
            (_arg("b", "logical"), "bool b"),
            (_arg("x", "real"), "float x"),
            (_arg("u", "real", rank=2, kind="dp"), "atmofab::View<const dp, 2> u"),
            (_arg("u", "real", rank=3, kind="dp", intent="out"), "atmofab::View<dp, 3> u"),
            (_arg("s", "string", len="assumed"), "const std::string& s"),
            (_arg("s", "string", len="4", intent="out"), "std::string& s"),
            (_arg("t", "derived", name="T"), "const T& t"),
            (_arg("t", "derived", name="T", intent="inout"), "T& t"),
            (_arg("v", "string", rank=1, len="assumed"), "const std::vector<std::string>& v"),
            (_arg("v", "derived", rank=1, name="T", intent="out"), "std::vector<T>& v"),
        ]
        for arg, want in rows:
            with self.subTest(want=want):
                self.assertEqual(want, self._param(arg))

    def test_results_components_parameters_and_prototypes(self) -> None:
        fn = {"kind": "function", "name": "d__g", "args": [],
              "result": {"name": "s", "spec": {"type": "string", "len": "deferred",
                                               "alloc": True}}}
        self.assertEqual("std::string d__g();\n", cs.render_symbol(fn))
        self.assertEqual("void d__h();\n", cs.render_symbol(
            {"kind": "subroutine", "name": "d__h", "args": []}))
        tdef = {"name": "T", "components": [
            {"name": "v", "spec": {"type": "real", "kind": "dp"}},
            {"name": "xs", "rank": 1, "spec": {"type": "derived", "name": "U", "alloc": True}}]}
        self.assertEqual("struct T {\n    dp v;\n    std::vector<U> xs;\n};\n", cs.render_symbol(tdef))
        self.assertEqual("using dp = double;", cs.render_module_parameter({"name": "dp", "value": "float64"}))
        self.assertEqual("using sp = float;", cs.render_module_parameter({"name": "sp", "value": "float32"}))
        self.assertEqual("inline constexpr int n = 64;",
                         cs.render_module_parameter({"name": "n", "value": 64}))
        proto = {"kind": "subroutine", "name": "rhs", "args": [_arg("t", "real", kind="dp")]}
        self.assertEqual("using rhs = void (*)(\n    dp t);\n", cs.render_interface(proto))
        with_proc = {"kind": "subroutine", "name": "d__a",
                     "args": [{"name": "f", "spec": {"type": "procedure", "interface": "rhs"}}]}
        self.assertEqual("rhs f", self._param(with_proc["args"][0]))

    def test_a_shape_with_no_lowering_is_refused(self) -> None:
        refused = [
            {"kind": "subroutine", "name": "d__a", "args": [_arg("b", "logical", kind="k")]},
            {"kind": "subroutine", "name": "new", "args": []},
            {"kind": "subroutine", "name": "d__a", "args": [_arg("class", "integer")]},
            {"name": "this", "components": []},
        ]
        for sig in refused:
            with self.subTest(sig=sig.get("name")), self.assertRaises(cs.SignatureParseError):
                cs.render_symbol(sig)

    def test_arrays_of_rank_two_and_more_that_own_their_storage_lower_to_array(self) -> None:
        """Every shape Fortran renders but a keyword name and a logical kind has a C++ lowering
        (the target-free Compile renders §5.1 in both): rank >= 2 owning arrays are the header's
        `atmofab::Array<X, R>`."""
        self.assertEqual("const atmofab::Array<std::string, 2>& v", self._param(
            _arg("v", "string", rank=2, len="assumed")))
        self.assertEqual("atmofab::Array<dp, 2>& v", self._param(
            _arg("v", "real", rank=2, kind="dp", intent="inout", alloc=True)))
        self.assertEqual("struct T {\n    atmofab::Array<dp, 2> a;\n};\n", cs.render_symbol(
            {"name": "T", "components": [{"name": "a", "rank": 2,
                                          "spec": {"type": "real", "kind": "dp", "alloc": True}}]}))
        self.assertEqual("atmofab::Array<dp, 3> d__r();\n", cs.render_symbol(
            {"kind": "function", "name": "d__r", "args": [],
             "result": {"name": "r", "rank": 3, "spec": {"type": "real", "kind": "dp"}}}))
        self.assertIn("struct Array {", cpp_header.VIEW_DEFINITION)

    def test_rank_one_numeric_arrays_that_own_their_storage_lower_to_vector(self) -> None:
        self.assertEqual("std::vector<dp>& v", self._param(
            _arg("v", "real", rank=1, kind="dp", intent="out", alloc=True)))
        self.assertEqual("const std::vector<dp>& v", self._param(
            _arg("v", "real", rank=1, kind="dp", alloc=True)))
        self.assertEqual("struct T {\n    std::vector<dp> a;\n};\n", cs.render_symbol(
            {"name": "T", "components": [{"name": "a", "rank": 1,
                                          "spec": {"type": "real", "kind": "dp", "alloc": True}}]}))

    def test_a_kind_naming_an_integer_valued_parameter_is_refused(self) -> None:
        """Only a float-valued module parameter lowers to a C++ TYPE (`using dp = double;`); an
        integer-valued one is a constant, and a kind naming it is refused in the whole-block
        render the Compile gate makes. A kind naming no parameter renders as the name."""
        good = {"module_parameters": [{"name": "dp", "value": "float64"}], "types": [],
                "interfaces": [], "procedures": [
                    {"kind": "subroutine", "name": "d__a", "args": [_arg("x", "real", kind="dp")]}]}
        self.assertIn("void d__a(", cs.render_signatures(good))
        self.assertIn("dp x", cs.render_signatures(dict(good, module_parameters=[])))
        with self.assertRaises(cs.SignatureParseError):
            cs.render_signatures(dict(good, module_parameters=[{"name": "dp", "value": "8"}]))


class CorpusRoundTripTests(unittest.TestCase):
    def test_every_section51_in_the_tree_renders_and_reads_back(self) -> None:
        """The target-free Compile gate renders every §5.1 in every language that declares
        `signatures`, so a §5.1 in this tree that C++ could not lower would fail every Compile.
        Each renders, reads back to exactly its symbols, and each symbol rendered alone is one
        stanza with the same atoms."""
        specs = sorted(p for p in (REPO_ROOT / "spec").rglob("controlled_spec.md") if _section51(p))
        self.assertGreaterEqual(len(specs), 13)
        for path in specs:
            with self.subTest(spec=str(path.relative_to(REPO_ROOT))):
                struct, err = cs.load_structured_signatures(_section51(path))
                self.assertIsNone(err)
                ops, types, ifaces, errors = cs.parse_interface_stanzas(cs.render_signatures(struct))
                self.assertEqual([], errors)
                self.assertEqual({p["name"] for p in struct["procedures"]}, set(ops))
                self.assertEqual({t["name"] for t in struct["types"]}, set(types))
                self.assertEqual({i["name"] for i in struct["interfaces"]}, set(ifaces))
                for sig in struct["procedures"] + struct["types"]:
                    o, t, _i, e = cs.parse_interface_stanzas(cs.render_symbol(sig))
                    self.assertEqual([], e)
                    (name, lines), = {**o, **t}.items()
                    self.assertEqual(sig["name"], name)
                    self.assertEqual(cs.stanza_line_list({**ops, **types}[name]),
                                     cs.stanza_line_list(lines))


_HARNESS_LIKE = {
    "module_parameters": [{"name": "dp", "value": "float64"}, {"name": "n", "value": "64"}],
    "types": [{"name": "h__rec", "components": [
        {"name": "id", "spec": {"type": "string", "len": "deferred", "alloc": True}},
        {"name": "v", "spec": {"type": "real", "kind": "dp"}}]}],
    "interfaces": [{"kind": "subroutine", "name": "h__cb", "args": [_arg("t", "real", kind="dp")]}],
    "procedures": [
        {"kind": "subroutine", "name": "h__run",
         "args": [_arg("u", "real", rank=1, kind="dp", intent="inout"),
                  {"name": "cb", "spec": {"type": "procedure", "interface": "h__cb"}},
                  _arg("ok", "logical", intent="out")]},
        {"kind": "function", "name": "h__emit", "args": [_arg("x", "real", kind="dp")],
         "result": {"name": "s", "spec": {"type": "string", "len": "deferred", "alloc": True}}},
    ],
}

def _public_api(struct: dict) -> dict:
    """An IR `public_api` carrying `struct` (the shape the header renderer reads)."""
    return {"signatures": [{"symbol": s["name"], "signature": s}
                           for s in struct["types"] + struct["procedures"]],
            "interfaces": [{"name": i["name"], "signature": i} for i in struct["interfaces"]],
            "module_parameters": struct["module_parameters"]}


_HEADER = cpp_header.render("h", _public_api(_HARNESS_LIKE), spec_kind="infrastructure")

_GOOD_MODEL = """#include "h_model.cuh"
namespace h_model {
void h__run(atmofab::View<dp, 1> u, h__cb cb, bool& ok) { (void)u; cb(0.0); ok = true; }
std::string h__emit(dp x) { (void)x; return "0"; }
}  // namespace h_model
"""


class RoundOneWitnessTests(unittest.TestCase):
    """Mechanisms round 1's reviewers found unpinned (a mutant of each survived)."""

    def test_a_qualified_unnamed_parameter_type_keeps_its_qualification(self) -> None:
        self.assertEqual(("ns::Type", ""), cpp_decls.parse_param("ns::Type"))

    def test_force_inline_and_a_trailing_return_type(self) -> None:
        decls = cpp_decls.read("namespace m {\n__forceinline__ int f(int a) { return a; }\n"
                               "auto g(int a) -> double { return a; }\n}\n")
        returns = {f.name: f.returns for f in decls.functions}
        self.assertEqual({"f": "int", "g": "double"}, returns)

    def test_a_forward_struct_declaration_is_not_a_variable(self) -> None:
        decls = cpp_decls.read("namespace m {\nstruct Later;\nint v;\n}\n")
        self.assertEqual(["v"], [v.name for v in decls.variables])

    def test_a_prototype_declared_twice_is_an_error(self) -> None:
        _o, _t, _i, errors = cs.parse_interface_stanzas(
            "using p = void (*)(int a);\nusing p = void (*)(int a);\n")
        self.assertTrue(any("prototype 'p' is declared more than once" in e for e in errors),
                        errors)

    def test_an_identifier_ending_in_R_before_a_quote_does_not_open_a_raw_string(self) -> None:
        text = 'call(aR"(", 1); int y{0};\n'
        masked = cpp_lines.mask(text)
        self.assertIn("int y{0};", masked)
        self.assertEqual('call(aR" ", 1); int y{0};\n', masked)

    def test_the_case_id_floor_reads_an_inequality_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "h_model.cu"
            model.write_text('if (case_id != "a") { metrics[0] = x; }\n')
            out: list[str] = []
            cpp_source.model_source_gates(node_key="infrastructure/h@0.1.0", model_file=model,
                                          text=model.read_text(), dep_spec_ids=[],
                                          violations=out, multidim_spec_id=None)
        self.assertTrue(any("hardcoded case_id -> metrics" in v for v in out), out)

    def test_the_snapshot_schema_name_is_exempt(self) -> None:
        out: list[str] = []
        cpp_source.validate_runner_snapshot_filenames(
            Path("r.cu"), 'std::ofstream f("raw/state_snapshots/snapshot_schema.json");\n', out)
        self.assertEqual([], out)

    def test_the_compiler_version_is_the_first_versioned_line(self) -> None:
        """The build derivation key's `compiler_version`: a driver that prints its NAME first
        must still be keyed by its release (the server's `_syntax_compiler_version`)."""
        import sys
        sys.path.insert(0, str(REPO_ROOT / "mcp_servers"))
        import build_runtime_server as server
        banner = (sys.executable, "-c",
                  ("print('driver: a compiler'); print('Copyright 2005-2026');"
                   "print('tools, release 13.4, V13.4.92')"))
        self.assertEqual("tools, release 13.4, V13.4.92", server._syntax_compiler_version(banner))
        plain = (sys.executable, "-c", "print('GNU Fortran 11.4.0'); print('Copyright 1.2')")
        self.assertEqual("GNU Fortran 11.4.0", server._syntax_compiler_version(plain))
        bare = (sys.executable, "-c", "print('no version here')")
        self.assertEqual("no version here", server._syntax_compiler_version(bare))


class HeaderTests(unittest.TestCase):
    def test_the_header_declares_the_whole_surface_in_the_model_namespace(self) -> None:
        decls = cpp_decls.read(_HEADER)
        self.assertEqual([], decls.errors)
        ns = ("h_model",)
        self.assertEqual({"h__run", "h__emit"},
                         {f.name for f in decls.functions if f.namespace == ns})
        self.assertFalse(any(f.defined for f in decls.functions))
        self.assertEqual(["h__rec"], [s.name for s in decls.structs if s.namespace == ns])
        self.assertEqual({"dp", "h__cb"}, {a.name for a in decls.aliases if a.namespace == ns})
        self.assertEqual(["n"], [v.name for v in decls.variables if v.namespace == ns])
        self.assertIn(cpp_header.VIEW_DEFINITION, _HEADER)
        self.assertTrue(_HEADER.splitlines()[2] == "#pragma once")
        self.assertEqual("h_model.cuh", cpp_header.basename("h"))

    def test_an_unlowerable_surface_raises(self) -> None:
        with self.assertRaises(cs.SignatureParseError):
            cpp_header.render("h", {"signatures": [{"symbol": "new", "signature": {
                "kind": "subroutine", "name": "new", "args": []}}]}, spec_kind="component")
        with self.assertRaises(cs.SignatureParseError):
            cpp_header.render("h", {"signatures": [{"symbol": "x"}]}, spec_kind="component")


class GeneratedSourcePinTests(unittest.TestCase):
    def _violations(self, model: str, name: str = "h_model.cu",
                    header: str | None = _HEADER) -> list[str]:
        ops, types, ifaces, errors = cs.parse_interface_stanzas(cs.render_signatures(_HARNESS_LIKE))
        self.assertEqual([], errors)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_text(model, encoding="utf-8")
            if header is not None:
                (Path(tmp) / "h_model.cuh").write_text(header, encoding="utf-8")
            out: list[str] = []
            cs.generated_source_violations(
                model_files=[path], target=path, ir_kind="infrastructure", op_stanzas=ops,
                type_stanzas=types, proto_stanzas=ifaces,
                module_parameters=_HARNESS_LIKE["module_parameters"],
                procedures=_HARNESS_LIKE["procedures"], violations=out)
            return [v.replace(tmp, "<tmp>") for v in out]

    def test_a_faithful_model_passes_whatever_its_spacing(self) -> None:
        self.assertEqual([], self._violations(_GOOD_MODEL))
        respaced = _GOOD_MODEL.replace("bool& ok", "bool &ok").replace(
            "atmofab::View<dp, 1> u", "atmofab::View< dp,1 >  u")
        self.assertNotEqual(respaced, _GOOD_MODEL)
        self.assertEqual([], self._violations(respaced))

    def test_a_directive_after_a_form_feed_is_not_read_as_code(self) -> None:
        """The pin's reader treats any preprocessing whitespace before `#` as the start of a
        directive, as the compiler does (round 3 of the review: with `[ \\t]*` there, the include
        below was read as code and both operations as never defined)."""
        self.assertEqual([], self._violations("\f" + _GOOD_MODEL))
        self.assertEqual([], self._violations("\v" + _GOOD_MODEL))

    def test_a_top_level_const_and_an_unnamed_forward_declaration_are_the_same_function(self) -> None:
        """`void f(const dp x)` declares `void f(dp)`, and a declaration may leave its
        parameters unnamed; both are one function with the header's declaration."""
        variant = _GOOD_MODEL.replace("std::string h__emit(dp x)", "std::string h__emit(const dp x)")
        variant = variant.replace(
            "namespace h_model {\n", "namespace h_model {\nvoid h__run(atmofab::View<dp, 1>, h__cb, bool&);\n")
        self.assertNotEqual(variant, _GOOD_MODEL)
        self.assertEqual([], self._violations(variant))

    def test_each_drift_is_named(self) -> None:
        run = "void h__run(atmofab::View<dp, 1> u, h__cb cb, bool& ok)"
        emit_def = 'std::string h__emit(dp x) { (void)x; return "0"; }'
        cases = {
            "argument order": (_GOOD_MODEL.replace(run, "void h__run(h__cb cb, atmofab::View<dp, 1> u, bool& ok)"),
                               "different signatures"),
            "argument name": (_GOOD_MODEL.replace("h__cb cb, bool& ok) { (void)u; cb(0.0)",
                                                  "h__cb f, bool& ok) { (void)u; f(0.0)"),
                              "drifts from"),
            "argument type": (_GOOD_MODEL.replace("std::string h__emit(dp x)", "std::string h__emit(double x)"),
                              "`std::string(dp)`; `std::string(double)`"),
            "device specifier": (_GOOD_MODEL.replace("std::string h__emit", "__device__ std::string h__emit"),
                                 "different signatures"),
            "vendor attribute": (_GOOD_MODEL.replace("std::string h__emit",
                                                     "__attribute__((device)) std::string h__emit"),
                                 "never DEFINED"),
            "declared only": (_GOOD_MODEL.replace(emit_def, ""), "never DEFINED"),
            "outside the namespace": (_GOOD_MODEL.replace(
                emit_def + "\n}  // namespace h_model",
                "}  // namespace h_model\n" + emit_def), "never DEFINED"),
            "prototype defined": (_GOOD_MODEL.replace(
                "}  // namespace h_model", "void h__cb(dp t) { (void)t; }\n}  // namespace h_model"),
                "DEFINES 'h__cb'"),
            "parameter again": (_GOOD_MODEL.replace(
                "namespace h_model {\n", "namespace h_model {\ninline constexpr int n = 64;\n"),
                "is bound 2 times"),
            "type again": (_GOOD_MODEL.replace(
                "namespace h_model {\n", "namespace h_model {\nstruct h__rec { int x; };\n"),
                "defined more than once"),
        }
        for label, (model, needle) in cases.items():
            with self.subTest(label=label):
                self.assertNotEqual(model, _GOOD_MODEL, "the fixture edit did not apply")
                found = self._violations(model)
                self.assertTrue(any(needle in v for v in found), (needle, found))

    def test_a_header_that_is_not_the_surface_is_named(self) -> None:
        """What the host wrote is pinned too: a header not rendered from this node's IR drifts."""
        cases = {
            "parameter": (_HEADER.replace("inline constexpr int n = 64;", "inline constexpr int n = 32;"),
                          "module parameter declaration `inline constexpr int n = 64;` is not"),
            "member": (_HEADER.replace("    dp v;\n", "    dp v;\n    int extra;\n"),
                       "type 'h__rec'"),
            "prototype": (_HEADER.replace("using h__cb = void (*)(\n    dp t);",
                                          "using h__cb = void (*)(\n    double t);"),
                          "prototype 'h__cb'"),
            "declaration": (_HEADER.replace("void h__run(", "void h__run_x("),
                            "procedure 'h__run' is not declared"),
        }
        for label, (header, needle) in cases.items():
            with self.subTest(label=label):
                self.assertNotEqual(header, _HEADER, "the fixture edit did not apply")
                found = self._violations(_GOOD_MODEL.replace("h__run(", "h__run_x(")
                                         if label == "declaration" else _GOOD_MODEL, header=header)
                self.assertTrue(any(needle in v for v in found), (needle, found))

    def test_a_missing_header_is_named(self) -> None:
        found = self._violations(_GOOD_MODEL, header=None)
        self.assertTrue(any("is not beside the model source" in v for v in found), found)

    def test_a_type_defined_twice_is_an_error(self) -> None:
        _o, _t, _i, errors = cs.parse_interface_stanzas(
            "struct T { int a; };\nnamespace { }\nstruct T { int a; };\n")
        self.assertTrue(any("type 'T' is defined more than once" in e for e in errors), errors)

    def test_one_publisher_only(self) -> None:
        ops, types, ifaces, _e = cs.parse_interface_stanzas(cs.render_signatures(_HARNESS_LIKE))
        out: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.cu", Path(tmp) / "b.cu"
            a.write_text(_GOOD_MODEL)
            b.write_text("")
            cs.generated_source_violations(
                model_files=[a, b], target=a, ir_kind="infrastructure", op_stanzas=ops,
                type_stanzas=types, proto_stanzas=ifaces, module_parameters=[], procedures=[],
                violations=out)
        self.assertTrue(any("exactly one is expected" in v for v in out), out)


def _fixed(arg_name, rank=1, intent="in", dims=("3",), **spec) -> dict:
    """A `real(dp)` argument with explicit `dims` (rank 0 when `rank` is 0)."""
    ent = _arg(arg_name, "real", rank=rank, intent=intent, kind="dp", **spec)
    if rank:
        ent["dims"] = list(dims)
    return ent


#: Issue #380: a pointwise `component` surface — every argument a scalar or a fixed-extent view.
_POINTWISE = _struct(
    module_parameters=[{"name": "dp", "value": "float64"}],
    procedures=[{"kind": "subroutine", "name": "c__flux",
                 "args": [_fixed("u"), _fixed("g", rank=0), _fixed("f", intent="out")]},
                {"kind": "subroutine", "name": "c__field",
                 "args": [_fixed("u", dims=("nx",)), _fixed("f", intent="out", dims=("nx",))]}])

_POINTWISE_MODEL = """#include "c_model.cuh"
namespace c_model {
__host__ __device__ void c__flux(atmofab::View<const dp, 1> u, dp g, atmofab::View<dp, 1> f) {
  for (int k = 0; k < 3; ++k) { f.data[k] = g * u.data[k]; }
}
void c__field(atmofab::View<const dp, 1> u, atmofab::View<dp, 1> f) { f.data[0] = u.data[0]; }
}  // namespace c_model
"""


class DeviceCallablePointwiseTests(unittest.TestCase):
    """Issue #380 (B1): a pointwise operation of a `component` node is declared — and must be
    defined — `__host__ __device__`, so a consumer's kernel may call it."""

    def test_which_procedures_are_pointwise(self) -> None:
        def sub(*args):
            return {"kind": "subroutine", "name": "c__f", "args": list(args)}
        rows = {
            "scalars and fixed views": (sub(_fixed("u"), _fixed("g", rank=0)), True),
            "integer and logical scalars": (sub(_arg("n", "integer"), _arg("b", "logical")), True),
            "rank 2, every extent a literal": (sub(_fixed("m", rank=2, dims=("3", "3"))), True),
            "a symbolic extent": (sub(_fixed("u", dims=("nx",))), False),
            "dims missing": (sub(_arg("u", "real", rank=1, kind="dp")), False),
            "dims shorter than the rank": (sub(_fixed("m", rank=2, dims=("3",))), False),
            "allocatable": (sub(_fixed("u", alloc=True)), False),
            "a string": (sub(_fixed("g", rank=0), _arg("s", "string", len="assumed")), False),
            "a derived value": (sub(_arg("t", "derived", name="T")), False),
            "a procedure argument": (sub({"name": "cb", "spec": {"type": "procedure",
                                                                 "interface": "p"}}), False),
            "no argument": (sub(), False),
            "a scalar function": ({"kind": "function", "name": "c__f", "args": [_fixed("u")],
                                   "result": {"name": "r", "spec": {"type": "real"}}}, True),
            "a function returning an array": (
                {"kind": "function", "name": "c__f", "args": [_fixed("u")],
                 "result": {"name": "r", "rank": 1, "spec": {"type": "real"}}}, False),
            "a function returning a string": (
                {"kind": "function", "name": "c__f", "args": [_fixed("u")],
                 "result": {"name": "r", "spec": {"type": "string", "len": "deferred",
                                                  "alloc": True}}}, False),
        }
        for label, (proc, want) in rows.items():
            with self.subTest(label):
                self.assertIs(want, cs.is_pointwise(proc))

    def test_the_rusanov_flux_section_is_pointwise(self) -> None:
        """The node that motivated the binding, read from the tree."""
        path = (REPO_ROOT / "spec/component/dynamics/shallow_water"
                "/dynamics_shallow_water_flux_2d_rusanov/controlled_spec.md")
        struct, err = cs.load_structured_signatures(_section51(path))
        self.assertIsNone(err)
        self.assertEqual([True], [cs.is_pointwise(p) for p in struct["procedures"]])

    def test_only_a_component_header_declares_the_pair(self) -> None:
        component = cpp_header.render("c", _public_api(_POINTWISE), spec_kind="component")
        self.assertIn("__host__ __device__ void c__flux(", component)
        self.assertIn("\nvoid c__field(", component)
        self.assertEqual(1, component.count("__host__ __device__ void"))
        for kind in ("problem", "infrastructure"):
            with self.subTest(kind):
                other = cpp_header.render("c", _public_api(_POINTWISE), spec_kind=kind)
                self.assertNotIn("__device__ void", other)
                self.assertIn("\nvoid c__flux(", other)
        decls = cpp_decls.read(component)
        self.assertEqual([], decls.errors)
        self.assertEqual({"c__flux": "__device__ void", "c__field": "void"},
                         {f.name: f.returns for f in decls.functions})

    def _pin(self, model: str, kind: str) -> list[str]:
        ops, types, ifaces, errors = cs.parse_interface_stanzas(cs.render_signatures(_POINTWISE))
        self.assertEqual([], errors)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c_model.cu"
            path.write_text(model, encoding="utf-8")
            (Path(tmp) / "c_model.cuh").write_text(
                cpp_header.render("c", _public_api(_POINTWISE), spec_kind=kind), encoding="utf-8")
            out: list[str] = []
            cs.generated_source_violations(
                model_files=[path], target=path, ir_kind=kind, op_stanzas=ops, type_stanzas=types,
                proto_stanzas=ifaces, module_parameters=_POINTWISE["module_parameters"],
                procedures=_POINTWISE["procedures"], violations=out)
            return out

    def test_the_pin_holds_the_definition_to_the_header_s_pair(self) -> None:
        self.assertEqual([], self._pin(_POINTWISE_MODEL, "component"))
        host_only = _POINTWISE_MODEL.replace("__host__ __device__ void c__flux", "void c__flux")
        cases = {
            # The definition lacks the pair the header declares: two signatures under one name.
            "a plain host definition": (host_only, "component", "different signatures"),
            # The pair on an operation the header declares a plain host function.
            "the pair on a non-pointwise operation": (
                _POINTWISE_MODEL.replace("void c__field", "__host__ __device__ void c__field"),
                "component", "different signatures"),
            # A `problem` node's header declares no pair, so its definition must carry none.
            "the pair on a problem node": (_POINTWISE_MODEL, "problem", "different signatures"),
        }
        for label, (model, kind, needle) in cases.items():
            with self.subTest(label):
                found = self._pin(model, kind)
                self.assertTrue(any(needle in v for v in found), found)
        self.assertEqual([], self._pin(host_only, "problem"))

    def test_the_pin_s_expected_stanza_carries_the_pair(self) -> None:
        """With the header and the model BOTH spelling a plain host function — a header not
        rendered for a `component` — the pin still refuses: what it expects of a component's
        pointwise operation comes from §5.1, not from the header beside the source."""
        ops, types, ifaces, _e = cs.parse_interface_stanzas(cs.render_signatures(_POINTWISE))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c_model.cu"
            path.write_text(_POINTWISE_MODEL.replace("__host__ __device__ void", "void"))
            (Path(tmp) / "c_model.cuh").write_text(
                cpp_header.render("c", _public_api(_POINTWISE), spec_kind="problem"))
            out: list[str] = []
            cs.generated_source_violations(
                model_files=[path], target=path, ir_kind="component", op_stanzas=ops,
                type_stanzas=types, proto_stanzas=ifaces,
                module_parameters=_POINTWISE["module_parameters"],
                procedures=_POINTWISE["procedures"], violations=out)
        self.assertTrue(any("procedure 'c__flux' drifts" in v
                            and "requires the definition to carry both" in v for v in out), out)

    def test_the_validator_hands_the_pin_section_5_1_s_procedures(self) -> None:
        """Through `_validate_generated_signatures` on a `cuda_cpp` component (round 1 of #380
        PR-1's review: the wiring `procedures=_section51_procedures(...)` could be replaced by
        `[]` with every row green, and then neither spelling of the pointwise operation passes —
        the component's Generate.static never converges). A faithful model passes; a plain host
        definition is refused."""
        import json

        import yaml

        from tools import validate_pipeline_semantics as vps
        from tools.tests.target_fixtures import (
            install_target_profile,
            pipe_ref,
            profile_with,
        )

        def run(model: str) -> list[str]:
            with tempfile.TemporaryDirectory() as t:
                repo = Path(t)
                (repo / "cs.md").write_text(
                    "## 5. Public API\nprose.\n### 5.1 Canonical interface block\n```yaml\n"
                    + yaml.safe_dump(_POINTWISE, sort_keys=False) + "```\n## 6. x\n")
                ir_dir = repo / "workspace" / "ir" / "x"
                ir_dir.mkdir(parents=True)
                (ir_dir / "spec.ir.yaml").write_text(json.dumps({
                    "meta": {"spec_kind": "component", "spec_id": "c",
                             "source_refs": {"controlled_spec": "cs.md"}},
                    "public_api": _public_api(_POINTWISE)}))
                install_target_profile(repo, profile_with(
                    hardware={"class": "gpu"},
                    toolchain={"language": "cuda_cpp", "compiler": "nvcc"},
                    parallel={"backend": "cuda"}))
                pipe = repo / pipe_ref("component__c__0.1.0", "c_20260101_001")
                src = pipe / "src"
                src.mkdir(parents=True)
                (pipe / "lineage.json").write_text(json.dumps({"ir_ref": "workspace/ir/x"}))
                (src / "c_model.cu").write_text(model)
                (src / "c_model.cuh").write_text(
                    cpp_header.render("c", _public_api(_POINTWISE), spec_kind="component"))
                execution = vps.NodeExecution(node_key="component/c@0.1.0", node_dir=pipe,
                                              exec_dir=pipe, pipeline_dir=pipe)
                out: list[str] = []
                vps._validate_generated_signatures(repo, execution, [src / "c_model.cu"], out)
                return out

        self.assertEqual([], run(_POINTWISE_MODEL))
        refused = run(_POINTWISE_MODEL.replace("__host__ __device__ void", "void"))
        self.assertTrue(any("c__flux" in v for v in refused), refused)

    def test_the_presence_floor_asks_a_pointwise_model_for_its_none_plan(self) -> None:
        """A pointwise operation's body may hold a counted loop over its fixed extent and launches
        nothing, so the floor refuses it under a plan that names CUDA (round 1 of #380 PR-1's
        review, measured) and passes it under the `"model": "none"` plan the producer is told to
        declare for it — the escape rule (6a) names is the one this gate honours."""
        import json

        from tools import validate_pipeline_semantics as vps
        from tools.tests.target_fixtures import (
            install_target_profile,
            pipe_ref,
            profile_with,
        )

        def floor(model: str) -> list[str]:
            with tempfile.TemporaryDirectory() as t:
                repo = Path(t)
                install_target_profile(repo, profile_with(
                    hardware={"class": "gpu"},
                    toolchain={"language": "cuda_cpp", "compiler": "nvcc"},
                    parallel={"backend": "cuda"}))
                pipe = repo / pipe_ref("component__c__0.1.0", "c_20260101_001")
                src = pipe / "source" / "src_20260101_001" / "src"
                src.mkdir(parents=True)
                (src.parent / "codegen_bundle.json").write_text(json.dumps({
                    "target_lowering_plan": {"precision": {}, "state_residency": "host",
                                             "parallelization": {"model": model}}}))
                (src / "c_model.cu").write_text(_POINTWISE_MODEL)
                out: list[str] = []
                vps._validate_parallel_presence_floor(
                    repo, vps._stub_execution(pipe, "component/c@0.1.0"), src,
                    [src / "c_model.cu"], out)
                return out

        self.assertGreater(cpp_source.counted_loops(_POINTWISE_MODEL), 0)
        self.assertTrue(any("not one kernel" in v for v in floor("cuda")), floor("cuda"))
        self.assertEqual([], floor("none"))

    def test_a_consumer_is_shown_the_pair_unqualified(self) -> None:
        facts = cs.published_interface(_POINTWISE_MODEL, "c__flux")
        self.assertEqual("__host__ __device__ void c__flux(atmofab::View<const c_model::dp,1> u, "
                         "c_model::dp g, atmofab::View<c_model::dp,1> f)", facts["interface"])
        self.assertEqual("void c__field(atmofab::View<const c_model::dp,1> u, "
                         "atmofab::View<c_model::dp,1> f)",
                         cs.published_interface(_POINTWISE_MODEL, "c__field")["interface"])
        self.assertEqual(["c__flux", "c__field"],
                         cs.prefixed_procedures(_POINTWISE_MODEL, "c__"))
        self.assertEqual(["c__field", "c__flux"],
                         sorted(cpp_source.published_subroutines(_POINTWISE_MODEL, "c")))


class DependencyInterfaceTests(unittest.TestCase):
    def test_published_interface_and_prefixed_procedures(self) -> None:
        facts = cs.published_interface(_GOOD_MODEL, "h__run")
        self.assertEqual(["u", "cb", "ok"], facts["argument_order"])
        self.assertEqual([1, 0, 0], [a["rank"] for a in facts["arguments"]])
        # A consumer's sources declare no `dp` and no `h__cb`: each header name is shown
        # qualified by the dependency's namespace, and what is already qualified stays as it is.
        self.assertEqual(["atmofab::View<h_model::dp,1>", "h_model::h__cb", "bool&"],
                         [a["type"] for a in facts["arguments"]])
        self.assertEqual("std::string h__emit(h_model::dp x)",
                         cs.published_interface(_GOOD_MODEL, "h__emit")["interface"])
        self.assertIsNone(cs.published_interface(_GOOD_MODEL, "h__absent"))
        self.assertEqual(["h__run", "h__emit"], cs.prefixed_procedures(_GOOD_MODEL, "h__"))
        self.assertEqual("p", cs.procedure_interface({"procedure_interface": " p "}))
        self.assertIsNone(cs.procedure_interface({}))
        self.assertEqual([], cs.argument_detail_lines(None))
        self.assertEqual(["    - `u`: `View` (rank 1)"],
                         cs.argument_detail_lines([{"name": "u", "type": "View", "rank": 1},
                                                   {"name": "x", "type": "dp"}]))


class SourceGateTests(unittest.TestCase):
    def test_the_preprocessor_allowlist(self) -> None:
        """Each spelling round 1 of the review used to silence a lint finding past the old
        denylist is refused, as are the conditional and the macro the §5.1 pin could not see
        through; the two permitted directives, and every forbidden spelling inside a comment or a
        literal, are not."""
        refused = {
            "diagnostic pragma": "#pragma GCC diagnostic ignored \"-Wunused-parameter\"\n",
            "nv pragma": "#pragma nv_diag_suppress 177\n",
            "continued pragma": "#pragma GCC diag\\\nnostic ignored \"-Wall\"\n",
            "digraph directive": "%:pragma nv_diag_suppress 177\n",
            "operator on two lines": "_Pragma\n(\"nv_diag_suppress 177\")\n",
            "pasted operator": "#define CAT(a,b) a##b\nCAT(_Pra,gma)(\"nv_diag_suppress 177\")\n",
            "linemarker": "# 1 \"/usr/include/fake.h\" 3\n",
            "line directive": "#line 10 \"x.cu\"\n",
            "hd warning": "#pragma hd_warning_disable\n",
            "exec check": "#pragma nv_exec_check_disable\n",
            "conditional": "#if 0\nint hidden();\n#endif\n",
            "macro": "#define NS h_model\n",
            "once": "#pragma once\n",
            "__pragma": "__pragma(warning(disable:1))\n",
            "brace digraph": "namespace h_model <% void f(); %>\n",
            "bracket digraph": "int a<:2:>;\n",
            # Round 2 of the review: each of these passed the round-1 allowlist.
            "form-feed directive": "\f#pragma nv_diag_suppress 177\n",
            "vertical-tab directive": "\v#pragma GCC diagnostic ignored \"-Wall\"\n",
            "form-feed conditional": "\f#if 0\nint hidden();\n\f#endif\n",
            "indented pragma": "\t  #pragma GCC diagnostic ignored \"-Wall\"\n",
            "operator split by a continuation": "_Pra\\\ngma(\"nv_diag_suppress 177\")\n",
            "operator after a dot-number": "x += .5'0; _Pragma(\"nv_diag_suppress 177\")\n",
            "include with a tail": "#include \"h_model.cuh\" junk\n",
            "pasting outside a directive": "int x = a ## b;\n",
            "closing bracket digraph": "int a[2:>;\n",
            # Round 3 of the review: each of these passed the round-2 allowlist.
            "comment opened across a splice": "#include \"h_model.cuh\" /\\\n*\nvoid f() {} // */\n",
            "comment closed across a splice": "/* c *\\\n/ int x;\n",
            "splice with trailing blanks": "int x; // a \\  \nint y;\n",
            "splice in a literal": "const char* s = \"a\\\nb\";\n",
            "runtime suppression macro": "__NV_SILENCE_DEPRECATION_BEGIN\n",
            "library pragma macro": "_PSTL_PRAGMA(nv_diag_suppress 177)\n",
            "cccl suppression macro": "_CCCL_DIAG_SUPPRESS_NVCC(177)\n",
            "glibc pragma macro": "__glibc_macro_warning1(GCC diagnostic ignored \"-Wall\")\n",
            "reserved name in an unroll": "#pragma unroll __NV_SILENCE_DEPRECATION_BEGIN\n",
            "non-standard header": "#include <cuda/std/cstddef>\n",
            "non-standard header with blanks": "#include < thrust/version.h >\n",
        }
        for label, text in refused.items():
            with self.subTest(label=label):
                self.assertTrue(cpp_source.preprocessor_violations(Path("x.cu"), text), text)
        self.assertEqual([], cpp_source.preprocessor_violations(
            Path("x.cu"),
            '#include "h_model.cuh"\n#include <cstdio>\n  #  include <vector>\n'
            "#pragma unroll\n#pragma unroll 4\n#pragma unroll (4)\n#pragma unroll kUnroll\n"
            "// #pragma GCC diagnostic ignored\n"
            "const char* s = \"#pragma nv_diag_suppress ## %: <%\";\n"
            "/*\n#pragma GCC diagnostic ignored \"-Wall\"\n*/\n"
            "// _Pragma(\"GCC diagnostic ignored\")\n"
            "std::vector<::std::string> v;\nint y = a ? b : c;\n"
            "#include <cuda_runtime.h>\n#include < cmath >\n"
            "__global__ void k(float* __restrict__ p) { __syncthreads(); p[0] = 1'0; }\n"
            "std::string h__emit(dp x);\nconst char* f = __func__;\n"))

    def test_a_digit_separator_in_a_number_starting_with_a_dot(self) -> None:
        text = "x = .5'0; y = 1e+1'0; int z{0};\n"
        self.assertEqual(text, cpp_lines.mask(text))

    def test_only_a_printf_family_format_is_read_as_a_format(self) -> None:
        out: list[str] = []
        cpp_source.validate_runner_json_serialization(
            Path("r.cu"), 'std::puts("coverage: 100% accurate");\n'
            'std::snprintf(buf, sizeof buf, "%a", x);\nstd::fprintf(f, "%.16e", x);\n', out)
        self.assertEqual(1, len(out), out)
        self.assertIn(":2:", out[0])

    def test_the_format_is_the_first_literal_of_the_call(self) -> None:
        """Round 3 of the review: `printf("%s", "100% Accurate")` (lowercased by the validator to
        `100% accurate`, i.e. `% a`) was refused. A later literal is an argument; a literal
        concatenated onto the first is still the format; a call with no literal ends at its own
        `)`, so the next call's literal is not read as its format."""
        cases = {
            'printf("%s\\n", "status: 100% accurate");\n': 0,
            'printf("%.16e " "%a", x);\n': 1,
            'printf(fmt, x); puts("100% accurate");\n': 0,
            'fprintf(f, "%a", x);\n': 1,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                out: list[str] = []
                cpp_source.validate_runner_json_serialization(Path("r.cu"), text, out)
                self.assertEqual(expected, len(out), out)

    def test_every_leaf_source_at_any_depth_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            for name in ("h_model.cu", "sub/quiet.cu", "h_model.cuh", "notes.txt"):
                (root / name).write_text("")
            (root / "link.cu").symlink_to(root / "h_model.cu")
            self.assertEqual([root / "h_model.cu", root / "sub" / "quiet.cu"],
                             cpp_source.leaf_sources(root))

    def test_counted_loops_read_code_only(self) -> None:
        text = ("for (int i = 0; i < n; ++i) {}\nfor (auto& x : v) {}\n"
                "// for (int j = 0; j < n; ++j)\nfor (int k = 0; f(k, 1); ++k) {}\n")
        self.assertEqual(2, cpp_source.counted_loops(text))
        self.assertEqual(1, cpp_source.counted_loops("for (i = 0; i < size+1'000; ++i) {}\n"))

    def test_runner_json_serialization(self) -> None:
        out: list[str] = []
        cpp_source.validate_runner_json_serialization(
            Path("r.cu"), 'printf("%.16e %d", x, n);\nprintf("%a", x);\nstd::cout << std::hexfloat;\n'
            '// printf("%a")\n'.lower(), out)
        self.assertEqual(2, len(out), out)
        self.assertTrue(any(":2:" in v and "%a" in v for v in out))
        self.assertTrue(any(":3:" in v and "hexfloat" in v for v in out))

    def test_runner_snapshot_filenames(self) -> None:
        text = ('std::ofstream f("raw/state_snapshots/snap_0001.json");\n'
                'std::ofstream g("raw/state_snapshots/" + case_id + ".json");\n'
                'std::ofstream h("raw/state_snapshots/case_a.json");\n'
                'std::string doc = "raw/state_snapshots/other.json";\n')
        out: list[str] = []
        cpp_source.validate_runner_snapshot_filenames(Path("r.cu"), text, out, {"case_a"})
        self.assertEqual(1, len(out), out)
        self.assertIn("snap_0001", out[0])

    def test_model_source_gates(self) -> None:
        literal = "".join(f"metrics[{i}] = 1.{i};\n" for i in range(6))
        branch = 'if (case_id == "a") { metrics[0] = x; }\n'
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "h_model.cu"
            model.write_text(literal + branch)
            (Path(tmp) / "sub").mkdir()
            (Path(tmp) / "sub" / "quiet.cu").write_text("#pragma GCC diagnostic ignored \"-Wall\"\n")
            out: list[str] = []
            cpp_source.model_source_gates(node_key="infrastructure/h@0.1.0", model_file=model,
                                          text=model.read_text(), dep_spec_ids=[], violations=out,
                                          multidim_spec_id=None)
            joined = "\n".join(out)
            self.assertIn("many literal metric assignments", joined)
            self.assertIn("hardcoded case_id -> metrics", joined)
            self.assertIn("quiet.cu:1: preprocessor directive `#pragma GCC diagnostic", joined)
            # Every `.cu` sits at the top level (round 3 of the review: the syntax stage never
            # compiled a nested one, which Build compiles as an object of its own).
            self.assertIn("quiet.cu: a CUDA C++ source in a subdirectory is refused", joined)
            self.assertNotIn("h_model.cu: a CUDA C++ source in a subdirectory", joined)
            self.assertNotIn("not implemented", joined)
            # A physics node's model gates RUN since R4-b PR-6 (they were a refusal before): a
            # component model with no defect draws nothing.
            out = []
            cpp_source.model_source_gates(node_key="component/c@0.1.0", model_file=model,
                                          text="int x;\n", dep_spec_ids=[], violations=out,
                                          multidim_spec_id=None)
            self.assertFalse([v for v in out if "h_model.cu" in v and "sub" not in v], out)
            self.assertFalse(any("not implemented" in v for v in out), out)

    def test_the_harness_has_nothing_to_check(self) -> None:
        out: list[str] = []
        cpp_source.validate_dependency_operations([Path("m.cu")], [], out)
        self.assertEqual([], out)
        self.assertEqual({"a": set()}, cpp_source.source_module_deps([Path("a.cu")]))
        self.assertEqual(["h__run", "h__emit"],
                         cpp_source.published_subroutines(_GOOD_MODEL, "h"))


_CHECKS_SOURCE = """#include "p_checks.cuh"

namespace p_checks {
double s = 0.0;
std::vector<double> u{};
atmofab::Array<double, 2> a2;

void case_setup(const std::string& case_id, bool& ok) { (void)case_id; ok = true; }
void case_run(const std::string& case_id, int& steps, int& cells_updated, bool& ok) {
  (void)case_id; steps = 1; cells_updated = 1; ok = true;
}
void get_time(double& t) { t = 0.0; }
void checks_compute(const std::string& case_id, const std::string& check_id,
                    std::string& status) { (void)case_id; (void)check_id; status = "na  "; }
void metric_compute(const std::string& case_id, const std::string& name, double& val,
                    bool& is_na, std::string& reason_na, bool& found) {
  (void)case_id; (void)name; val = 0.0; is_na = false; reason_na = ""; found = false;
}
}  // namespace p_checks
"""

_DEP_HEADER = """namespace dep_model {
void dep__flux(atmofab::View<const double, 1> u, atmofab::View<double, 1> f, double dt);
double dep__norm(const std::vector<double>& u);
}
"""


#: Issue #314: the model a reaching checks source calls, and that checks source — the problem
#: idiom (the checks source DECLARES the operation in the model's namespace).
_P_MODEL = """namespace p_model {
void p__step(std::vector<double>& u, double& s) { u[0] = s; }
}
"""
_REACHING_CHECKS = _CHECKS_SOURCE.replace(
    "namespace p_checks {",
    "namespace p_model {\nvoid p__step(std::vector<double>& u, double& s);\n}\n\nnamespace p_checks {",
    1).replace("  (void)case_id; steps = 1;", "  p_model::p__step(u, s);\n  (void)case_id; steps = 1;")
_REACH = "case_run reaches no published operation of the model"


class ChecksReachGateTests(unittest.TestCase):
    """Issue #314: `checks_model_reach_violations`, the C++ reading. Each row is named for the
    mutant it kills; the handler rows live in `test_validate_pipeline_semantics`."""

    def _run(self, checks: str, model: str | None = _P_MODEL) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            models = []
            if model is not None:
                (Path(tmp) / "p_model.cu").write_text(model)
                models = [Path(tmp) / "p_model.cu"]
            return cpp_source.checks_model_reach_violations(
                Path(tmp) / "p_checks.cu", checks, models, "p")

    def _with_run_body(self, body: str, extra: str = "") -> str:
        return _REACHING_CHECKS.replace("  p_model::p__step(u, s);\n", body).replace(
            "}  // namespace p_checks", extra + "}  // namespace p_checks")

    def test_the_reaching_idiom_passes_every_checks_gate(self) -> None:
        self.assertEqual([], self._run(_REACHING_CHECKS))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "p_model.cu").write_text(_P_MODEL)
            self.assertEqual([], cpp_source.checks_harness_isolation_violations(
                Path(tmp) / "p_checks.cu", _REACHING_CHECKS, [Path(tmp) / "p_model.cu"]))

    def test_m1_a_case_run_computing_the_state_inline_is_refused(self) -> None:
        v = self._run(self._with_run_body("  u[0] = s;\n"))
        self.assertEqual(len(v), 1, v)
        self.assertIn(_REACH, v[0])
        self.assertIn("(one of: p__step)", v[0])

    def test_m2_a_model_defining_no_operation_is_refused(self) -> None:
        for label, model in (("none", None),
                             ("outside the namespace", ("void p__step(std::vector<double>& u, "
                                                        "double& s) { u[0] = s; }\n")),
                             ("without the prefix", _P_MODEL.replace("p__step", "step")),
                             ("declared only", "namespace p_model {\nvoid p__step(int);\n}\n")):
            with self.subTest(label):
                v = self._run(_REACHING_CHECKS, model)
                self.assertEqual(len(v), 1, v)
                self.assertIn("the model source defines no `p__<op>` in `namespace p_model`", v[0])

    def test_m3_only_the_checks_namespace_case_run_is_the_root(self) -> None:
        for label, checks in (
                ("from case_setup", self._with_run_body("").replace(
                    "(void)case_id; ok = true; }", "(void)case_id; p_model::p__step(u, s); ok = true; }")),
                ("from an orphan helper", self._with_run_body(
                    "", "void orphan() { p_model::p__step(u, s); }\n")),
                ("from a case_run in another namespace", self._with_run_body("") + (
                    "namespace other {\nvoid case_run(const std::string& c, int& a, int& b, bool& o) "
                    "{ p_model::p__step(p_checks::u, p_checks::s); }\n}\n"))):
            with self.subTest(label):
                v = self._run(checks)
                self.assertEqual([_REACH in x for x in v], [True], v)

    def test_m4_a_helper_case_run_reaches_is_followed(self) -> None:
        for label, body, extra in (
                ("helper", "  advance();\n", "void advance() { p_model::p__step(u, s); }\n"),
                ("unnamed namespace", "  advance();\n",
                 "namespace {\nvoid advance() { p_model::p__step(u, s); }\n}\n"),
                ("two levels", "  outer();\n",
                 "void inner() { p_model::p__step(u, s); }\nvoid outer() { inner(); }\n"),
                ("template arguments at the call", "  advance<atmofab::View<double, 1>>(u);\n",
                 "void advance(std::vector<double>& v) { p_model::p__step(v, s); }\n"),
                ("value-returning helper", "  const double r = norm_after(u);\n  (void)r;\n",
                 ("double norm_after(std::vector<double>& v) { p_model::p__step(v, s); "
                  "return v[0]; }\n"))):
            with self.subTest(label):
                self.assertEqual([], self._run(self._with_run_body(body, extra)))
                v = self._run(self._with_run_body("", extra))
                self.assertEqual([_REACH in x for x in v], [True], v)

    def test_m6_a_checks_definition_of_an_operation_is_a_shadow(self) -> None:
        for label, extra in (
                ("overload in the model namespace",
                 ("}  // namespace p_checks\nnamespace p_model {\nvoid p__step(int n) { (void)n; }\n"
                  "}\nnamespace p_checks {\n")),
                ("in the checks namespace", "void p__step(int n) { (void)n; }\n")):
            with self.subTest(label):
                v = self._run(self._with_run_body("  p_model::p__step(u, s);\n", extra))
                self.assertTrue(any("defines `p__step`, a published operation of the model" in x
                                    for x in v), v)
        v = self._run(self._with_run_body("  dep_model::dep__flux(u);\n"))
        self.assertEqual([_REACH in x for x in v], [True], v)

    def test_m7_a_comment_or_a_literal_is_not_a_reach(self) -> None:
        for label, body in (("comment", "  // p_model::p__step(u, s);\n"),
                            ("block comment", "  /* p__step(u, s); */\n"),
                            ("literal", '  const char* note = "p__step(u, s)";\n  (void)note;\n'),
                            ("using only", "  using p_model::p__step;\n")):
            with self.subTest(label):
                v = self._run(self._with_run_body(body))
                self.assertEqual([_REACH in x for x in v], [True], v)

    def test_a_checks_namespace_without_case_run_is_refused(self) -> None:
        v = self._run(_REACHING_CHECKS.replace("void case_run(", "void case_go("))
        self.assertEqual(len(v), 1, v)
        self.assertIn("namespace p_checks defines no `case_run`", v[0])

    def test_m9_an_unbalanced_source_is_refused_not_passed(self) -> None:
        v = self._run(_REACHING_CHECKS + "void broken() {\n")
        self.assertEqual(len(v), 1, v)
        self.assertIn("the checks-reach gate cannot read this source's declarations", v[0])

    def test_m10_every_spelling_of_a_reach_is_followed(self) -> None:
        for label, body in (
                ("qualified", "  p_model::p__step(u, s);\n"),
                ("split over lines", "  p_model::\n      p__step(\n u, s);\n"),
                ("taken by address", ("  void (*op)(std::vector<double>&, double&) = "
                                      "&p_model::p__step;\n  op(u, s);\n")),
                ("inside a lambda", "  auto go = [&]() { p_model::p__step(u, s); };\n  go();\n")):
            with self.subTest(label):
                self.assertEqual([], self._run(self._with_run_body(body)))

    def test_a_namespace_alias_of_the_model_qualifies(self) -> None:
        """Certified sources call through `namespace md = <spec_id>_model;`."""
        alias = _REACHING_CHECKS.replace("namespace p_checks {",
                                         "namespace md = p_model;\nnamespace p_checks {", 1)
        self.assertEqual([], self._run(alias.replace("p_model::p__step(u, s);",
                                                     "md::p__step(u, s);")))

    def test_a_local_entity_named_as_the_operation_is_refused(self) -> None:
        """Round 1 (issue #314): a lambda, functor, variable, member or template carrying the
        operation's name, and an unqualified call that may reach one, look like a call to the
        model and run the checks source's own update. The name appears only qualified by the
        model's namespace, or as the checks source's declaration in it."""
        fake = "[&](std::vector<double>& v, double& x) { v[0] = x; }"
        for label, body, extra in (
                ("local lambda", f"  auto p__step = {fake};\n  p__step(u, s);\n", ""),
                ("namespace-scope lambda", "  p__step(u, s);\n",
                 f"auto p__step = {fake.replace('[&]', '[]')};\n"),
                ("local functor", ("  struct F { void operator()(std::vector<double>& v, double& x)"
                                   " { v[0] = x; } } p__step;\n  p__step(u, s);\n"), ""),
                ("local variable", "  int p__step = 1;\n  (void)p__step;\n", ""),
                ("local struct static member",
                 ("  struct W { static void p__step(std::vector<double>& v, double& x) "
                  "{ v[0] = x; } };\n  W::p__step(u, s);\n"), ""),
                ("alias-named local struct",
                 ("  struct p_model { static void p__step(std::vector<double>& v, double& x) "
                  "{ v[0] = x; } };\n  p_model::p__step(u, s);\n"), ""),
                ("after a using-declaration", "  using p_model::p__step;\n  p__step(u, s);\n", ""),
                ("after a using-directive", "  using namespace p_model;\n  p__step(u, s);\n", ""),
                ("template in the model namespace", "  p_model::p__step(u, s);\n",
                 ("}  // namespace p_checks\nnamespace p_model {\ntemplate <typename T = int>\n"
                  "void p__step(std::vector<double>& v, double& x) { v[0] = x; }\n}\n"
                  "namespace p_checks {\n"))):
            with self.subTest(label):
                v = self._run(self._with_run_body(body, extra))
                self.assertTrue(any("other than as `p_model::<op>`" in x for x in v), v)
                if label in ("local lambda", "local functor", "local struct static member"):
                    # ...and the call to it is no reach either.
                    self.assertTrue(any(_REACH in x for x in v), v)

    def test_only_the_models_namespace_qualifies(self) -> None:
        v = self._run(self._with_run_body("  other::p__step(u, s);\n"))
        self.assertTrue(any(_REACH in x for x in v), v)
        self.assertTrue(any("other than as `p_model::<op>`" in x for x in v), v)
        # A declaration of the name outside the model's namespace is not the problem idiom.
        v = self._run(self._with_run_body(
            "  p_model::p__step(u, s);\n", "void p__step(std::vector<double>& u, double& s);\n"))
        self.assertTrue(any("other than as `p_model::<op>`" in x for x in v), v)

    def test_the_host_rendered_runner_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "p_model.cu").write_text(_P_MODEL)
            (Path(tmp) / "p_runner.cu").write_text("void p__step(int);\n")
            v = cpp_source.checks_model_reach_violations(
                Path(tmp) / "p_checks.cu", _REACHING_CHECKS, [Path(tmp) / "p_model.cu"], "p")
            self.assertEqual(v, [])
            # ...but a leaf file of the runner's NAME in a subdirectory is read (round 3).
            (Path(tmp) / "sub").mkdir()
            (Path(tmp) / "sub" / "p_runner.cu").write_text("void p__step(int);\n")
            v = cpp_source.checks_model_reach_violations(
                Path(tmp) / "p_checks.cu", _REACHING_CHECKS, [Path(tmp) / "p_model.cu"], "p")
        self.assertEqual(len(v), 1, v)
        self.assertIn("sub/p_runner.cu: names a published operation", v[0])

    def test_a_helper_source_naming_the_operation_is_refused(self) -> None:
        """A helper `.cu` the checks source includes is read by the same rule."""
        helper = ("namespace p_model {\ntemplate <typename T = int>\n"
                  "void p__step(std::vector<double>& v, double& x) { v[0] = x; }\n}\n")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "p_model.cu").write_text(_P_MODEL)
            (Path(tmp) / "fake.cu").write_text(helper)
            v = cpp_source.checks_model_reach_violations(
                Path(tmp) / "p_checks.cu", _REACHING_CHECKS, [Path(tmp) / "p_model.cu"], "p")
        self.assertEqual(len(v), 1, v)
        self.assertIn("fake.cu: names a published operation of the model", v[0])

    def test_forms_not_followed_are_refused(self) -> None:
        """Documented refusals (GENERATE_RULES.md §5): a struct member function, a namespace-scope
        lambda variable and a template function are not read as functions, so an operation
        reached only through one is no reach — over-refusal, never a pass."""
        for label, body, extra in (
                ("struct member", "  Stepper{}.go();\n",
                 "struct Stepper { void go() { p_model::p__step(u, s); } };\n"),
                ("namespace-scope lambda", "  run_step();\n",
                 "auto run_step = [] { p_model::p__step(u, s); };\n"),
                ("template function", "  advance<int>();\n",
                 "template <typename T> void advance() { p_model::p__step(u, s); }\n")):
            with self.subTest(label):
                v = self._run(self._with_run_body(body, extra))
                self.assertEqual([_REACH in x for x in v], [True], v)


class PhysicsGateTests(unittest.TestCase):
    """The checks-source, dependency-use and `problem` model gates a physics node reaches
    (R4-b PR-6): each passes the certified idiom and names each defect it exists for."""

    def test_a_clean_checks_source_passes_every_checks_gate(self) -> None:
        path = Path("p_checks.cu")
        self.assertEqual([], cpp_source.checks_module_declaration_violations(
            path, _CHECKS_SOURCE, "p"))
        published, subroutines, defined = cpp_source.checks_module_abi_facts(_CHECKS_SOURCE, "p")
        abi = set(cpp_checks_abi.CHECKS_PUBLIC_NAMES)
        self.assertEqual((abi, abi, abi), (published, subroutines, defined))
        self.assertEqual([], cpp_source.unpublished_bound_state(_CHECKS_SOURCE, "p",
                                                                ["s", "u", "a2"]))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], cpp_source.checks_harness_isolation_violations(
                Path(tmp) / path.name, _CHECKS_SOURCE, []))
            # The neutral caller passes what the renderer admits (issue #316): nothing, here.
            self.assertEqual([], cpp_source.checks_harness_isolation_violations(
                Path(tmp) / path.name, _CHECKS_SOURCE, [], allowed_harness_uses={}))
            with self.assertRaises(ValueError):
                cpp_source.checks_harness_isolation_violations(
                    Path(tmp) / path.name, _CHECKS_SOURCE, [],
                    allowed_harness_uses={"harness_cpp_gpu_model": {"x"}})

    def test_the_renderer_admits_no_physics_harness_use_and_no_distributed_state(
            self) -> None:
        """Issue #316: the two seam questions a distributed harness raises, answered for this
        language's one harness — the checks-source gate asks them of every host-rendered node."""
        from tools import host_render
        self.assertEqual(host_render.physics_harness_uses("cuda_cpp", "harness_cpp_gpu"), {})
        self.assertEqual(host_render.distributed_state_names("cuda_cpp", "harness_cpp_gpu",
                                                             ["u"]), [])

    def test_the_header_include_and_the_namespace_are_required(self) -> None:
        no_include = _CHECKS_SOURCE.replace('#include "p_checks.cuh"', "// #include \"p_checks.cuh\"")
        out = cpp_source.checks_module_declaration_violations(Path("c.cu"), no_include, "p")
        self.assertTrue(any('must `#include "p_checks.cuh"`' in v for v in out), out)
        other_ns = _CHECKS_SOURCE.replace("namespace p_checks {", "namespace q_checks {")
        out = cpp_source.checks_module_declaration_violations(Path("c.cu"), other_ns, "p")
        self.assertTrue(any("namespace p_checks" in v for v in out), out)
        unbalanced = _CHECKS_SOURCE + "void broken() {\n"
        out = cpp_source.checks_module_declaration_violations(Path("c.cu"), unbalanced, "p")
        self.assertTrue(any("cannot read this source's declarations" in v for v in out), out)
        self.assertEqual((set(), set(), set()),
                         cpp_source.checks_module_abi_facts(unbalanced, "p"))
        self.assertEqual(["s"], cpp_source.unpublished_bound_state(unbalanced, "p", ["s"]))

    def test_a_callback_is_published_only_as_the_header_declares_it(self) -> None:
        """Each way a definition misses the runner's call reads as unpublished: another
        parameter type (an overload), another return type, internal linkage, a device function,
        an unnamed namespace. A qualified definition outside the namespace block counts."""
        variants = {
            "float": _CHECKS_SOURCE.replace("void get_time(double& t)", "void get_time(float& t)"),
            "value": _CHECKS_SOURCE.replace("void get_time(double& t) { t = 0.0; }",
                                            "void get_time(double t) { (void)t; }"),
            "return": _CHECKS_SOURCE.replace("void get_time(double& t) { t = 0.0; }",
                                             "int get_time(double& t) { t = 0.0; return 0; }"),
            "static": _CHECKS_SOURCE.replace("void get_time(", "static void get_time("),
            "inline": _CHECKS_SOURCE.replace("void get_time(", "inline void get_time("),
            "device": _CHECKS_SOURCE.replace("void get_time(", "__device__ void get_time("),
            "unnamed": _CHECKS_SOURCE.replace("void get_time(double& t) { t = 0.0; }",
                                              "namespace { void get_time(double& t) { t = 0.0; } }"),
        }
        for label, text in variants.items():
            with self.subTest(label):
                published, _subs, _defined = cpp_source.checks_module_abi_facts(text, "p")
                self.assertNotIn("get_time", published)
                self.assertIn("case_setup", published)
        # Round 1 of this change's review: an east `const` is the same parameter type.
        east = _CHECKS_SOURCE.replace("void case_setup(const std::string& case_id",
                                      "void case_setup(std::string const& case_id")
        self.assertIn("case_setup", cpp_source.checks_module_abi_facts(east, "p")[0])
        # A declaration without a body defines nothing (the runner's call would not link).
        declared = _CHECKS_SOURCE.replace("void get_time(double& t) { t = 0.0; }",
                                          "void get_time(double& t);")
        facts = cpp_source.checks_module_abi_facts(declared, "p")
        self.assertNotIn("get_time", facts[0])
        self.assertNotIn("get_time", facts[2])
        qualified = _CHECKS_SOURCE.replace("void get_time(double& t) { t = 0.0; }", "") + (
            "void p_checks::get_time(double& when) { when = 0.0; }\n")
        self.assertIn("get_time", cpp_source.checks_module_abi_facts(qualified, "p")[0])

    def test_a_checks_declaration_of_the_node_s_operation_must_match_its_definition(self) -> None:
        """Round 3 of this change's review: a `problem` node's header declares nothing of its
        operation, so its checks source declares it itself; a declaration with other types than
        the model's definition is refused before Build, where it is a link error."""
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "p_model.cu"
            model.write_text("namespace p_model {\nvoid p__step(atmofab::View<double, 1> u, "
                             "int nx) { (void)u; (void)nx; }\n}\n")
            declared = _CHECKS_SOURCE + ("namespace p_model {\nvoid p__step("
                                         "atmofab::View<double, 1> u, int {t});\n}\n")
            for label, text, refused in (
                    ("same types, other name", declared.replace("{t}", "n"), False),
                    ("another type", declared.replace("int {t}", "long n"), True),
                    ("no such operation", declared.replace("p__step", "p__other").replace(
                        "{t}", "n"), True)):
                with self.subTest(label):
                    out = cpp_source.checks_harness_isolation_violations(
                        Path(tmp) / "p_checks.cu", text, [model])
                    self.assertEqual(refused, any("declares `p_model::" in v for v in out), out)

    def test_bound_state_must_be_defined_with_external_linkage(self) -> None:
        for label, text in {
            "static": _CHECKS_SOURCE.replace("double s = 0.0;", "static double s = 0.0;"),
            "const": _CHECKS_SOURCE.replace("double s = 0.0;", "const double s = 0.0;"),
            "extern": _CHECKS_SOURCE.replace("double s = 0.0;", "extern double s;"),
            "unnamed": _CHECKS_SOURCE.replace("double s = 0.0;", "namespace { double s = 0.0; }"),
            "absent": _CHECKS_SOURCE.replace("double s = 0.0;", ""),
        }.items():
            with self.subTest(label):
                self.assertEqual(["s"], cpp_source.unpublished_bound_state(text, "p", ["s", "u"]))
        direct = _CHECKS_SOURCE.replace("std::vector<double> u{};", "std::vector<double> u(3);")
        self.assertEqual([], cpp_source.unpublished_bound_state(direct, "p", ["u"]))
        # Round 1 of this change's review: every declarator of one statement is defined.
        several = _CHECKS_SOURCE.replace("double s = 0.0;", "double t0 = 1.0, s = 0.0;").replace(
            "std::vector<double> u{};", "std::vector<double> w, u;")
        self.assertEqual([], cpp_source.unpublished_bound_state(several, "p", ["s", "u", "a2"]))

    def test_isolation_refuses_the_harness_and_file_io(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checks = Path(tmp) / "p_checks.cu"
            model = Path(tmp) / "p_model.cu"
            model.write_text('#include "harness_cpp_gpu_model.cuh"\nint x;\n')
            out = cpp_source.checks_harness_isolation_violations(checks, _CHECKS_SOURCE, [model])
            self.assertTrue(any(str(model) in v and "harness" in v for v in out), out)
            for reference in ("void f() { harness_cpp_gpu_model::harness_cpp_gpu__emit_real(1.0); }",
                              "namespace h = harness_cpp_gpu_model;",
                              "void f() { harness_cpp_gpu__box(a, b); }"):
                model.write_text(reference + "\n")
                out = cpp_source.checks_harness_isolation_violations(checks, _CHECKS_SOURCE,
                                                                     [model])
                self.assertTrue(any(str(model) in v for v in out), (reference, out))
            model.write_text('// #include "harness_cpp_gpu_model.cuh"\n'
                             'const char* m = "harness_cpp_gpu_model::";\n')
            self.assertEqual([], cpp_source.checks_harness_isolation_violations(
                checks, _CHECKS_SOURCE, [model]))

    _IO_SPELLINGS = (
        "std::ofstream out(\"x\");", "FILE* f = fopen(\"x\", \"w\");",
        "std::fstream f(\"x\", std::ios::out);", "std::basic_ofstream<char> f(\"x\");",
        "std::wofstream f(\"x\");", "std::ifstream in(\"x\");", "std::system(\"cp a b\");",
        "popen(\"ls\", \"r\");", "std::filesystem::copy_file(\"a\", \"b\");",
        "std::rename(\"a\", \"b\");", "std::remove(\"a\");", "fopen64(\"x\", \"w\");",
        "freopen64(\"x\", \"w\", stdout);", "renameat(0, \"a\", 0, \"b\");",
        "unlink(\"x\");", "std::atexit(g);", "std::at_quick_exit(g);", "f.open(\"x\");")

    def test_every_io_name_is_refused_however_it_is_reached(self) -> None:
        """Round 3 of this change's review reached `fopen` through a function pointer and
        `filebuf::open` through a pointer to member. Every name of the two enumerations is
        refused bound to a pointer (the unambiguous ones) or qualified / called / taken by
        address (the ones that also name ordinary things) — one row per element, derived from
        the constants so an element added later is witnessed too."""
        # The rows below are DERIVED from the constants, so a member dropped from them drops its
        # row too: the round-5 additions (the process and descriptor calls past `system`) are
        # pinned as literals first.
        self.assertLessEqual(
            {"syscall", "posix_spawn", "posix_spawnp", "fork", "vfork", "mkstemp", "mkostemp",
             "symlink", "symlinkat", "linkat", "dup2", "dup3"}, set(cpp_source.LEAF_IO_NAMES))
        self.assertIn("link", cpp_source.LEAF_IO_CALL_NAMES)
        forms = {name: [f"auto p = &{name};", f"auto p = std::{name};"]
                 for name in cpp_source.LEAF_IO_NAMES if name != "asm"}
        forms["asm"] = ['asm("nop");']
        for name in cpp_source.LEAF_IO_CALL_NAMES:
            forms[name] = [f"auto p = &std::{name};", f"::{name}(x);", f"{name}(x);"]
        forms["filebuf"] = ["auto p = &std::filebuf::open;", "std::basic_filebuf<char> b;"]
        for name, spellings in forms.items():
            for spelling in spellings:
                with self.subTest(spelling), tempfile.TemporaryDirectory() as tmp:
                    (Path(tmp) / "p_model.cu").write_text(
                        f"namespace {{ void w() {{ {spelling} }} }}\n")
                    out = cpp_source.checks_harness_isolation_violations(
                        Path(tmp) / "p_checks.cu", _CHECKS_SOURCE, [])
                    self.assertTrue(any("p_model.cu" in v and "must not do file I/O" in v
                                        for v in out), out)
        # Round 5: a self-declared C symbol reached a system call past every name.
        for spelling in ('extern "C" long syscall(long, ...);', 'extern "C" { int f(int); }'):
            with self.subTest(spelling), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "p_model.cu").write_text(spelling + "\n")
                out = cpp_source.checks_harness_isolation_violations(
                    Path(tmp) / "p_checks.cu", _CHECKS_SOURCE, [])
                self.assertTrue(any("language linkage" in v for v in out), out)
        for clean in ("double system_size = 1.0;", "double rename_count = 0.0;",
                      "extern double shared_total;", "double linkage = 0.0;",
                      "void g(double& x) { x = other::open; }"):
            with self.subTest(clean), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "p_model.cu").write_text(clean + "\n")
                self.assertEqual([], cpp_source.checks_harness_isolation_violations(
                    Path(tmp) / "p_checks.cu", _CHECKS_SOURCE, []))

    def test_no_leaf_source_does_file_io_or_runs_after_main(self) -> None:
        """Round 2 of this change's review: a MODEL source's namespace-scope destructor rewrote
        `diagnostics.json` after the harness wrote it, and every gate passed. Every leaf `.cu`
        of the node is read, the checks source and a helper included; the host-rendered runner
        is not."""
        for io in self._IO_SPELLINGS:
            for victim in ("p_checks.cu", "p_model.cu", "p_helper.cu"):
                with self.subTest(io=io, file=victim), tempfile.TemporaryDirectory() as tmp:
                    checks = Path(tmp) / "p_checks.cu"
                    text = _CHECKS_SOURCE
                    body = f"namespace {{ void w() {{ {io} }} }}\n"
                    if victim == "p_checks.cu":
                        text = _CHECKS_SOURCE + body
                    else:
                        (Path(tmp) / victim).write_text(body)
                    out = cpp_source.checks_harness_isolation_violations(checks, text, [])
                    self.assertTrue(any(victim in v and "must not do file I/O" in v
                                        for v in out), out)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "p_runner.cu").write_text("std::ofstream host_rendered;\n")
            self.assertEqual([], cpp_source.checks_harness_isolation_violations(
                Path(tmp) / "p_checks.cu", _CHECKS_SOURCE, []))
        # Over-refusal probes: a name, a literal, the `<algorithm>` remove, a physics helper.
        for clean in ("double opened = 0.0;", "const char* m = \"std::system(x)\";",
                      "double removed_mass = 1.0;",
                      "void g(std::vector<int>& v) { v.erase(std::remove(v.begin(), v.end(), 0), "
                      "v.end()); }", "void apply_open_boundary(double& x) { x = 0.0; }",
                      "bool is_open = false;"):
            with self.subTest(clean), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual([], cpp_source.checks_harness_isolation_violations(
                    Path(tmp) / "p_checks.cu", _CHECKS_SOURCE + clean + "\n", []))

    def _model(self, tmp: str, body: str, *, header: bool = True) -> Path:
        if header:
            (Path(tmp) / "dep_model.cuh").write_text(_DEP_HEADER)
        model = Path(tmp) / "p_model.cu"
        model.write_text('#include "p_model.cuh"\n#include "dep_model.cuh"\n'
                         f"namespace p_model {{\n{body}\n}}\n")
        return model

    def _gates(self, model: Path, deps: list[str], *, multidim: str | None = None,
               node_key: str = "problem/p@0.1.0") -> list[str]:
        out: list[str] = []
        cpp_source.model_source_gates(node_key=node_key, model_file=model,
                                      text=model.read_text(), dep_spec_ids=deps, violations=out,
                                      multidim_spec_id=multidim)
        return [v for v in out if "p_model.cu" in v]

    def test_dependency_use_requires_include_call_and_no_redefinition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = self._model(tmp, "void p__run(double& x) { x = dep_model::dep__norm({}); }")
            out: list[str] = []
            cpp_source.validate_dependency_operations([model], ["dep", "other"], out)
            self.assertEqual([f"{model}: missing dependency header include "
                              f'(#include "other_model.cuh")',
                              f"{model}: missing dependency operation call (other__*)"], out)
            model = self._model(tmp, "// dep__norm(u) in a comment\n"
                                     "void dep__norm(double& x) { x = 0.0; }")
            out = []
            cpp_source.validate_dependency_operations([model], ["dep"], out)
            self.assertIn(f"{model}: dependency operation redefinition detected (dep__*)", out)
            self.assertIn(f"{model}: missing dependency operation call (dep__*)", out)

    def test_literal_outputs_are_refused_on_a_problem_node_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = self._model(tmp, "void p__lit(double& a, double& b, double x) "
                                     "{ a = 1.0; b = 2.5e-3; (void)x; }\n"
                                     "void p__ok(double& a, double x) { a = 2.0 * x; }")
            out = self._gates(model, [])
            self.assertEqual([f"{model}: function p__lit has literal-only assignments for all "
                              "output parameters"], out)
            self.assertEqual([], self._gates(model, [], node_key="component/p@0.1.0"))

    def test_the_literal_gate_s_each_clause(self) -> None:
        """Each clause of the literal-outputs gate, one row each (round 1 of this change's review:
        four of its clauses had no witness): a name made of literal-suffix letters is a name, an
        element write is not a whole assignment, every output must be assigned whole, an
        input-dependent right-hand side exempts, and a boolean literal is a literal."""
        cases = {
            "suffix-letter local": ("void p__f(atmofab::View<const double, 1> h, double& energy) "
                                    "{ double e = 0.0; for (long i = 0; i < h.extent[0]; ++i) "
                                    "{ e += h.data[i]; } energy = e; }", False),
            "element write": ("void p__f(atmofab::View<double, 1> u, double x) "
                              "{ u.data[0] = 1.0; (void)x; }", False),
            "one output not a literal": ("void p__f(double& a, double& b, double x) "
                                         "{ a = 1.0; b = b * x; }", False),
            "one output unassigned": ("void p__f(double& a, double& b, double x) "
                                      "{ a = 1.0; (void)b; (void)x; }", False),
            "boolean literal": ("void p__f(bool& ok, double& a, double x) "
                                "{ ok = true; a = 2.0; (void)x; }", True),
            "suffixed literal": ("void p__f(float& a, double x) { a = 0.5f; (void)x; }", True),
            "compound update": ("void p__f(double& x) { x += 1.0; }", False),
        }
        for label, (body, refused) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                out = self._gates(self._model(tmp, body), [])
                self.assertEqual(refused, any("literal-only assignments" in v for v in out), out)

    def test_a_discarded_dependency_output_is_refused(self) -> None:
        good = ("void p__step(atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,"
                " double dt) {\n"
                "  std::vector<double> flux(static_cast<std::size_t>(u.extent[0]));\n"
                "  atmofab::View<double, 1> fv{flux.data(), {u.extent[0]}};\n"
                "  dep_model::dep__flux(u, fv, dt);\n"
                "  for (long i = 0; i < u.extent[0]; ++i) {\n"
                "    u_new.data[i] = u.data[i] + dt * flux[static_cast<std::size_t>(i)];\n"
                "  }\n}")
        bad = good.replace("u.data[i] + dt * flux[static_cast<std::size_t>(i)]", "u.data[i]")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], self._gates(self._model(tmp, good), ["dep"]))
            model = self._model(tmp, bad)
            self.assertEqual([f"{model}: function p__step does not propagate dependency "
                              "operation outputs to its output dataflow (candidates=['fv'])"],
                             self._gates(model, ["dep"]))

    def test_a_declaration_s_initializer_is_not_an_assignment_before_the_call(self) -> None:
        """Round 1 of this change's review: copy-initialized buffers (`= std::vector<double>(n)`,
        `auto f = ...`) were read as inputs the call consumes, so a discarded dependency result
        passed. The Fortran binding's assignment pattern matches no declaration either."""
        for decl in ("std::vector<double> flux(4);", "std::vector<double> flux = "
                     "std::vector<double>(4);", "auto flux = std::vector<double>(4);"):
            body = ("void p__run(atmofab::View<const double, 1> u, double& out, double dt) {\n"
                    f"  {decl}\n"
                    "  dep_model::dep__flux(u, atmofab::View<double, 1>{flux.data(), {4}}, dt);\n"
                    "  out = dt;\n}")
            with self.subTest(decl), tempfile.TemporaryDirectory() as tmp:
                model = self._model(tmp, body)
                self.assertEqual([f"{model}: function p__run does not propagate dependency "
                                  "operation outputs to its output dataflow "
                                  "(candidates=['flux'])"], self._gates(model, ["dep"]))

    # Round 2 of this change's review: shapes of a dependency output the closure must follow.
    # Each is written twice — CONSUMED (the flux reaches `u_new`: the gate passes) and DISCARDED
    # (`u_new` copies `u`: the gate refuses) — so a row is red if the gate is blind in either
    # direction. `{flux}` in the templates is where the call's output actual goes.
    _CONSUME = ("  for (long i = 0; i < 4; ++i) {\n"
                "    u_new.data[i] = u.data[i] + dt * flux[static_cast<std::size_t>(i)];\n  }\n")
    _DISCARD = ("  for (long i = 0; i < 4; ++i) {\n    u_new.data[i] = u.data[i];\n  }\n")
    _SHAPES: ClassVar[dict[str, str]] = {
        "pointer": ("  double* fp = flux.data();\n"
                    "  dep_model::dep__flux(u, atmofab::View<double, 1>{fp, {4}}, dt);\n"),
        "auto view": ("  auto fv = atmofab::View<double, 1>{flux.data(), {4}};\n"
                      "  dep_model::dep__flux(u, fv, dt);\n"),
        "copy-initialized view": (
            "  atmofab::View<double, 1> fv = atmofab::View<double, 1>{flux.data(), {4}};\n"
            "  dep_model::dep__flux(u, fv, dt);\n"),
        "member-assigned view": ("  atmofab::View<double, 1> fv;\n  fv.data = flux.data();\n"
                                 "  fv.extent[0] = 4;\n  dep_model::dep__flux(u, fv, dt);\n"),
        "helper": ("  dep_model::dep__flux(u, as_view(flux), dt);\n"),
        "address": ("  dep_model::dep__flux(u, atmofab::View<double, 1>{&flux[0], {4}}, dt);\n"),
    }

    def _flux_model(self, tmp: str, shape: str, tail: str) -> Path:
        body = ("namespace {\natmofab::View<double, 1> as_view(std::vector<double>& v) {\n"
                "  return atmofab::View<double, 1>{v.data(), {static_cast<long>(v.size())}};\n}\n}\n"
                "void p__step(atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,"
                " double dt) {\n  std::vector<double> flux(4);\n" + shape + tail + "}")
        return self._model(tmp, body)

    def test_the_dataflow_follows_every_shape_of_a_dependency_output(self) -> None:
        for label, shape in self._SHAPES.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual([], self._gates(self._flux_model(tmp, shape, self._CONSUME),
                                                 ["dep"]))
                out = self._gates(self._flux_model(tmp, shape, self._DISCARD), ["dep"])
                self.assertEqual(1, len(out), out)
                self.assertIn("does not propagate dependency operation outputs", out[0])

    def test_the_dataflow_follows_a_device_round_trip(self) -> None:
        """The default GPU lowering: the dependency's output copied to the device, consumed by a
        kernel this file defines, and copied back (round 2 of this change's review: refused)."""
        kernel = ("__global__ void upd(const double* u, const double* f, double* out, double dt) {\n"
                  "  const int i = threadIdx.x;\n  out[i] = u[i] + dt * f[i];\n}\n")
        call = ("void p__step(atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,"
                " double dt) {\n  std::vector<double> flux(4);\n"
                "  dep_model::dep__flux(u, atmofab::View<double, 1>{flux.data(), {4}}, dt);\n"
                "  double* du = nullptr;\n  double* df = nullptr;\n  double* dn = nullptr;\n"
                "  cudaMemcpy(du, u.data, 32, cudaMemcpyHostToDevice);\n"
                "  cudaMemcpy(df, flux.data(), 32, cudaMemcpyHostToDevice);\n"
                "  upd<<<1, 4>>>(du, df, dn, dt);\n"
                "  cudaMemcpy(u_new.data, dn, 32, cudaMemcpyDeviceToHost);\n}")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], self._gates(self._model(tmp, kernel + call), ["dep"]))
            dropped = call.replace("cudaMemcpy(df, flux.data(), 32, cudaMemcpyHostToDevice);",
                                   "cudaMemcpy(df, u.data, 32, cudaMemcpyHostToDevice);")
            out = self._gates(self._model(tmp, kernel + dropped), ["dep"])
            self.assertTrue(any("does not propagate" in v for v in out), out)

    _KERNEL = ("__global__ void upd(const double* u, const double* f, double* {restrict}out,"
               " double dt) {{\n  const int i = threadIdx.x;\n  out[i] = u[i] + dt * {use};\n}}\n")
    _ROUND_TRIP = ("void p__step(atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,"
                   " double dt) {{\n  std::vector<double> flux(4);\n"
                   "  dep_model::dep__flux(u, atmofab::View<double, 1>{{flux.data(), {{4}}}}, dt);\n"
                   "  double* du = nullptr;\n  double* df = nullptr;\n  double* dn = nullptr;\n"
                   "  cudaMemcpy(du, u.data, 32, cudaMemcpyHostToDevice);\n"
                   "  cudaMemcpy(df, {copied}.data(), {count}, cudaMemcpyHostToDevice);\n"
                   "  {launch}(du, df, dn, dt);\n"
                   "  cudaMemcpy(u_new.data, dn, 32, cudaMemcpyDeviceToHost);\n}}")

    def _round_trip(self, tmp: str, *, restrict: str = "", use: str = "f[i]",
                    copied: str = "flux", count: str = "32",
                    launch: str = "upd<<<1, 4>>>") -> list[str]:
        kernel = self._KERNEL.format(restrict=restrict, use=use)
        if "<double>" in launch:
            kernel = "template <class T>\n" + kernel.replace("const double*", "const T*").replace(
                "double* ", "T* ", 1)
        body = kernel + self._ROUND_TRIP.format(copied=copied, count=count, launch=launch)
        return self._gates(self._model(tmp, body), ["dep"])

    def test_the_device_round_trip_is_followed_through_what_the_kernel_body_reads(self) -> None:
        """Round 3 of this change's review. Passes: a `__restrict__` output pointer (was read as an
        input). Refused: a kernel whose body ignores the flux (was credited from its signature),
        a copy whose byte COUNT is the flux's (was credited as data) — each of the latter two
        passed while the dependency result was discarded."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], self._round_trip(tmp))
            self.assertEqual([], self._round_trip(tmp, restrict="__restrict__ "))
            # A TEMPLATE kernel is not read (`declarations` skips templates), so its launch is not
            # followed: refused even when it consumes the flux — closed, as the rules say.
            out = self._round_trip(tmp, launch="upd<double><<<1, 4>>>")
            self.assertTrue(any("does not propagate" in v for v in out), out)
            for label, kwargs in (("kernel ignores f", {"use": "u[i]"}),
                                  ("count only", {"copied": "u", "count": "flux.size() * 8"})):
                with self.subTest(label):
                    out = self._round_trip(tmp, **kwargs)
                    self.assertTrue(any("does not propagate" in v for v in out), out)

    # Issue #380: a pointwise dependency declared `__host__ __device__`, called one face at a
    # time from inside a kernel the model defines — or from a host helper. `{use}` is what the
    # update kernel adds to `u`: the faces' flux (consumed) or nothing of it (discarded).
    _PW_HEADER = ("namespace dep_model {\n__host__ __device__ void dep__face(\n"
                  "    atmofab::View<const double, 1> u,\n    double g,\n"
                  "    atmofab::View<double, 1> f);\n}\n")
    _PW_FACES = ("__global__ void faces(const double* u, double* f, double g, long n) {\n"
                 "  const long i = blockIdx.x * blockDim.x + threadIdx.x;\n"
                 "  if (i < n) {\n"
                 "    dep_model::dep__face(atmofab::View<const double, 1>{u + 3 * i, {3}}, g,\n"
                 "                         atmofab::View<double, 1>{f + 3 * i, {3}});\n  }\n}\n")
    _PW_HELPER = ("void faces_on_host(const double* u, double* f, double g, long n) {\n"
                  "  for (long i = 0; i < n; ++i) {\n"
                  "    dep_model::dep__face(atmofab::View<const double, 1>{u + 3 * i, {3}}, g,\n"
                  "                         atmofab::View<double, 1>{f + 3 * i, {3}});\n  }\n}\n")
    _PW_UPDATE = ("__global__ void upd(const double* u, const double* f, double* out, double dt,"
                  " long n) {{\n  const long i = blockIdx.x * blockDim.x + threadIdx.x;\n"
                  "  if (i < n) {{ out[i] = u[i] + dt * {use}; }}\n}}\n")
    _PW_STEP = ("void p__step(atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,"
                " double dt) {{\n  double* du = nullptr;\n  double* df = nullptr;\n"
                "  double* dn = nullptr;\n"
                "  cudaMemcpy(du, u.data, 96, cudaMemcpyHostToDevice);\n"
                "  {faces}(du, df, 9.81, 4);\n  upd<<<1, 12>>>(du, df, dn, dt, 12);\n"
                "  cudaMemcpy(u_new.data, dn, 96, cudaMemcpyDeviceToHost);\n}}")

    def test_a_dependency_called_inside_a_kernel_is_followed_through_the_launch(self) -> None:
        """The kernel's own check exempts its output pointer (an output parameter IS an output),
        so the launch that hands it a buffer is what must be followed: before issue #380 the
        discarded variant passed, because `p__step` itself calls no dependency operation."""
        shapes = {"kernel": (self._PW_FACES, "faces<<<1, 4>>>"),
                  "host helper": (self._PW_HELPER, "faces_on_host")}
        for label, (callee, faces) in shapes.items():
            for header in (True, False):
                with self.subTest(label, header=header), tempfile.TemporaryDirectory() as tmp:
                    if header:
                        (Path(tmp) / "dep_model.cuh").write_text(self._PW_HEADER)

                    def gates(use: str, callee: str = callee, faces: str = faces) -> list[str]:
                        body = (callee + self._PW_UPDATE.format(use=use)
                                + self._PW_STEP.format(faces=faces))
                        return self._gates(self._model(tmp, body, header=False), ["dep"])
                    self.assertEqual([], gates("f[i]"))
                    out = gates("u[i]")
                    self.assertEqual(1, len(out), out)
                    self.assertIn("function p__step does not propagate dependency operation "
                                  "outputs to its output dataflow (candidates=['df'])", out[0])

    def test_assigned_before_is_measured_from_the_dependency_call_itself(self) -> None:
        """The "assigned before the call" exemption compares against THIS call's position (round
        0 of #380 PR-1: with the position taken from the last call of any kind in the body, the
        later `std::sqrt(...)` moved it past `flux[0] = 0.0;` and the overwritten result passed)."""
        body = ("void p__f(atmofab::View<const double, 1> u, double& out, double dt) {\n"
                "  std::vector<double> flux(4);\n"
                "  dep_model::dep__flux(u, atmofab::View<double, 1>{flux.data(), {4}}, dt);\n"
                "  flux[0] = 0.0;\n  out = std::sqrt(dt);\n}")
        with tempfile.TemporaryDirectory() as tmp:
            model = self._model(tmp, body)
            want = (f"{model}: function p__f does not propagate dependency operation "
                    "outputs to its output dataflow (candidates=['flux'])")
            self.assertEqual([want], self._gates(model, ["dep"]))

    # Round 1 of #380 PR-1's review: the shapes in which a callee's OTHER output stood for the
    # dependency result it discards. Each is written consuming (`f[i]`) and discarding (`0.0`)
    # the flux; the discarded one must be refused with the buffer the flux was written into.
    _PW_FLAGGED = ("__global__ void faces(const double* u, double* f, double g, long n,"
                   " {flag}* bad) {{\n"
                   "  const long i = blockIdx.x * blockDim.x + threadIdx.x;\n  if (i < n) {{\n"
                   "    bool gok = true;\n"
                   "    dep_model::dep__face_ok(atmofab::View<const double, 1>{{u + 3 * i, {{3}}}}, g,\n"
                   "        atmofab::View<double, 1>{{f + 3 * i, {{3}}}}, gok);\n"
                   "    if (!gok) {{ *bad = 1; }}\n  }}\n}}\n")
    _PW_FLAGGED_HEADER = ("namespace dep_model {\n__host__ __device__ void dep__face_ok(\n"
                          "    atmofab::View<const double, 1> u,\n    double g,\n"
                          "    atmofab::View<double, 1> f,\n    bool& ok);\n}\n")
    _PW_FLAGGED_STEP = ("void p__step(atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,"
                        " bool& ok) {{\n  double* du = nullptr;\n  double* df = nullptr;\n"
                        "  double* dn = nullptr;\n  {flag}* dbad = nullptr;\n  {flag} bad = 0;\n"
                        "  cudaMemcpy(du, u.data, 96, cudaMemcpyHostToDevice);\n"
                        "  {launch}(du, df, 9.81, 4, dbad);\n"
                        "  upd<<<1, 12>>>(du, df, dn, 0.1, 12);\n"
                        "  cudaMemcpy(u_new.data, dn, 96, cudaMemcpyDeviceToHost);\n"
                        "  cudaMemcpy(&bad, dbad, sizeof(bad), cudaMemcpyDeviceToHost);\n"
                        "  ok = !bad;\n}}")
    _PW_LAUNCHER = ("void launch_faces(const double* u, double* f, double g, long n, {flag}* bad) {{\n"
                    "  faces<<<1, 4>>>(u, f, g, n, bad);\n}}\n")

    def test_a_kernel_s_guard_flag_does_not_stand_for_the_flux_it_discards(self) -> None:
        """A kernel that passes its dependency's guard on through a flag pointer has two output
        parameters; only the one the dependency's flux reaches stands for the dependency call
        (before: the flag reaching `ok` made the launch "propagated" while the flux was
        dropped). Directly launched, and through a host helper that launches it — the helper
        carries the kernel's reach one level up (the fixed point)."""
        for flag in ("int", "bool"):
            for label, extra, launch in (("kernel", "", "faces<<<1, 4>>>"),
                                         ("helper", self._PW_LAUNCHER, "launch_faces"),
                                         ("helper defined first", self._PW_LAUNCHER,
                                          "launch_faces")):
                with self.subTest(flag=flag, via=label), tempfile.TemporaryDirectory() as tmp:
                    (Path(tmp) / "dep_model.cuh").write_text(self._PW_FLAGGED_HEADER)

                    def gates(use: str, flag: str = flag, extra: str = extra,
                              launch: str = launch, label: str = label) -> list[str]:
                        kernel = self._PW_FLAGGED.format(flag=flag)
                        if label == "helper defined first":
                            # The helper precedes the kernel it launches (a forward declaration
                            # names it), so one pass in file order sees the helper before the
                            # kernel's reach is known: the fixed point is what carries it.
                            head = kernel.split(" {\n", 1)[0] + ";\n"
                            kernel = head + extra.format(flag=flag) + kernel
                        else:
                            kernel += extra.format(flag=flag)
                        body = (kernel + self._PW_UPDATE.format(use=use)
                                + self._PW_FLAGGED_STEP.format(flag=flag, launch=launch))
                        return self._gates(self._model(tmp, body, header=False), ["dep"])
                    self.assertEqual([], gates("f[i]"))
                    out = gates("0.0")
                    self.assertEqual(1, len(out), out)
                    self.assertIn("function p__step does not propagate dependency operation "
                                  "outputs to its output dataflow (candidates=['df'])", out[0])

    def test_a_host_helper_s_scalar_output_does_not_stand_for_its_array(self) -> None:
        """A host helper filling a vector from the dependency and returning its guard through a
        `bool&`: a caller that reads only the flag discards the flux. (Were the flag computed
        from the flux, the flux would reach `ok` through the helper's summary, and the gate's
        closure rule would call it propagated.)"""
        helper = ("void flux_host(const std::vector<double>& u, std::vector<double>& f, bool& fok)"
                  " {\n  for (long i = 0; i < 4; ++i) {\n    bool gok = true;\n"
                  "    dep_model::dep__face_ok(atmofab::View<const double, 1>{u.data() + 3 * i, {3}},"
                  " 9.81,\n        atmofab::View<double, 1>{f.data() + 3 * i, {3}}, gok);\n"
                  "    fok = gok;\n  }\n}\n")
        step = ("void p__step(const std::vector<double>& u, std::vector<double>& u_new, bool& ok)"
                " {{\n  std::vector<double> fh(12);\n  bool fok = true;\n"
                "  flux_host(u, fh, fok);\n"
                "  for (long i = 0; i < 12; ++i) {{ u_new[i] = u[i] + {use}; }}\n  ok = fok;\n}}")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dep_model.cuh").write_text(self._PW_FLAGGED_HEADER)
            consumed = self._gates(self._model(tmp, helper + step.format(use="fh[i]"),
                                               header=False), ["dep"])
            self.assertEqual([], consumed)
            out = self._gates(self._model(tmp, helper + step.format(use="0.0"), header=False),
                              ["dep"])
            self.assertEqual(1, len(out), out)
            self.assertIn("(candidates=['fh'])", out[0])

    def test_a_helper_carrying_a_scalar_result_reads_only_its_output_positions(self) -> None:
        """A dependency with a SCALAR output only (no array to narrow to), carried by a host
        helper: what stands for it is the helper's output parameter, not the input the caller
        hands it — an input that also reaches the caller's output must not make the discarded
        speed look consumed."""
        header = ("namespace dep_model {\n__host__ __device__ void dep__speed(\n    double h,\n"
                  "    double& c);\n}\n")
        helper = ("void wave_speed(const std::vector<double>& w, double& c) {\n"
                  "  double cl = 0.0;\n  dep_model::dep__speed(w[0], cl);\n  c = cl;\n}\n")
        step = ("void p__f(atmofab::View<const double, 1> u, double& out) {{\n"
                "  std::vector<double> w(u.data, u.data + 3);\n  double c = 0.0;\n"
                "  wave_speed(w, c);\n  out = {use};\n}}")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dep_model.cuh").write_text(header)
            self.assertEqual([], self._gates(self._model(
                tmp, helper + step.format(use="w[0] + c"), header=False), ["dep"]))
            out = self._gates(self._model(tmp, helper + step.format(use="w[0]"), header=False),
                              ["dep"])
            self.assertEqual(1, len(out), out)
            self.assertIn("(candidates=['c'])", out[0])

    def test_an_index_does_not_stand_for_the_buffer_it_indexes(self) -> None:
        """`View<double, 1>{f + 3 * (base + i), {3}}` hands over `f`; the index is an integer
        local `i` and an integer parameter `base`, and an output computed from them (a kernel
        writing the face index it processed) must not stand for the flux — each of the two is
        what the row needs (either alone left unread makes `seen` stand for it)."""
        kernel = ("__global__ void faces(const double* u, double* f, double g, long n,"
                  " long* seen, long base) {\n"
                  "  const long i = blockIdx.x * blockDim.x + threadIdx.x;\n  if (i < n) {\n"
                  "    dep_model::dep__face(atmofab::View<const double, 1>{u + 3 * (base + i), {3}},"
                  " g,\n                         atmofab::View<double, 1>{f + 3 * (base + i), {3}});\n"
                  "    seen[i] = base + i;\n  }\n}\n")
        step = ("void p__step(atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,"
                " long& count) {{\n  double* du = nullptr;\n  double* df = nullptr;\n"
                "  double* dn = nullptr;\n  long* dseen = nullptr;\n"
                "  cudaMemcpy(du, u.data, 96, cudaMemcpyHostToDevice);\n"
                "  faces<<<1, 4>>>(du, df, 9.81, 4, dseen, 0);\n"
                "  upd<<<1, 12>>>(du, df, dn, 0.1, 12);\n"
                "  cudaMemcpy(u_new.data, dn, 96, cudaMemcpyDeviceToHost);\n"
                "  cudaMemcpy(&count, dseen, 8, cudaMemcpyDeviceToHost);\n}}")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dep_model.cuh").write_text(self._PW_HEADER)
            self.assertEqual([], self._gates(self._model(
                tmp, kernel + self._PW_UPDATE.format(use="f[i]") + step.format(), header=False),
                ["dep"]))
            out = self._gates(self._model(
                tmp, kernel + self._PW_UPDATE.format(use="0.0") + step.format(), header=False),
                ["dep"])
            self.assertEqual(1, len(out), out)
            self.assertIn("(candidates=['df'])", out[0])

    # Round 2 of #380 PR-1's review. `_PW2_HEADER`'s operation writes two arrays and a guard,
    # the shape of the rusanov flux (F_star, G_star, ok).
    _PW2_HEADER = ("namespace dep_model {\n__host__ __device__ void dep__face2(\n"
                   "    atmofab::View<const double, 1> u,\n    double g,\n"
                   "    atmofab::View<double, 1> f,\n    atmofab::View<double, 1> h,\n"
                   "    bool& ok);\n__host__ __device__ void dep__speed(\n    double h,\n"
                   "    double& c);\n}\n")
    _PW2_UPDATE = ("__global__ void upd(const double* u, const double* f, double* out, long n) {{\n"
                   "  const long i = threadIdx.x;\n  if (i < n) {{ out[i] = u[i] + {use}; }}\n}}\n")
    _PW2_STEP = ("void p__step(atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,"
                 " bool& ok) {\n  double* du = nullptr;\n  double* df = nullptr;\n"
                 "  double* dn = nullptr;\n  int* dbad = nullptr;\n  int bad = 0;\n"
                 "  cudaMemcpy(du, u.data, 96, cudaMemcpyHostToDevice);\n"
                 "  faces<<<1, 4>>>(du, df, 9.81, 4, dbad);\n  upd<<<1, 12>>>(du, df, dn, 12);\n"
                 "  cudaMemcpy(u_new.data, dn, 96, cudaMemcpyDeviceToHost);\n"
                 "  cudaMemcpy(&bad, dbad, 4, cudaMemcpyDeviceToHost);\n  ok = !bad;\n}")
    _PW2_HELPER = ("__device__ {ret} face_flux(const double* u, double* f, double g, long i) {{\n"
                   "  double fx[3];\n  double gy[3];\n  bool gok = true;\n"
                   "  dep_model::dep__face2(atmofab::View<const double, 1>{{u + 3 * i, {{3}}}}, g,\n"
                   "      atmofab::View<double, 1>{{fx, {{3}}}}, atmofab::View<double, 1>{{gy, {{3}}}},"
                   " gok);\n  for (int k = 0; k < 3; ++k) {{ f[3 * i + k] = fx[k]; }}\n"
                   "  (void)gy;\n  return {returned};\n}}\n")
    _PW2_KERNELS: ClassVar[dict[str, tuple[str, str, str]]] = {
        # A guard or a speed taken from what a __device__ helper RETURNS while it fills the flux.
        "returned guard": ("bool", "gok",
                           "  if (i < n) { bad[i] = face_flux(u, f, g, i) ? 0 : 1; }\n"),
        "returned guard, stored first": (
            "bool", "gok",
            ("  if (i < n) {\n    const bool fok = face_flux(u, f, g, i);\n"
             "    bad[i] = fok ? 0 : 1;\n  }\n")),
        "returned speed": ("double", "u[3 * i]",
                           "  if (i < n) { bad[i] = face_flux(u, f, g, i) > 0.0 ? 0 : 1; }\n"),
    }

    def _pw2(self, tmp: str, kernel: str, use: str) -> list[str]:
        body = kernel + self._PW2_UPDATE.format(use=use) + self._PW2_STEP
        return self._gates(self._model(tmp, body, header=False), ["dep"])

    def test_what_a_helper_returns_is_not_what_it_fills(self) -> None:
        """A guard (or any value) a `__device__` helper RETURNS is computed from its inputs, not
        from the buffer it fills: `bad[i] = face_flux(u, f, g, i) ? 0 : 1` must not make the
        flag stand for the flux in `f` (before: every argument of the call was a source of
        `bad`, so the flag carried `f` and a dropped flux passed)."""
        for label, (ret, returned, launch_body) in self._PW2_KERNELS.items():
            kernel = (self._PW2_HELPER.format(ret=ret, returned=returned)
                      + "__global__ void faces(const double* u, double* f, double g, long n,"
                      " int* bad) {\n  const long i = threadIdx.x;\n" + launch_body + "}\n")
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "dep_model.cuh").write_text(self._PW2_HEADER)
                self.assertEqual([], self._pw2(tmp, kernel, "f[i]"))
                out = self._pw2(tmp, kernel, "0.0")
                self.assertEqual(1, len(out), out)
                self.assertIn("function p__step does not propagate", out[0])
                self.assertIn("(candidates=['df'])", out[0])

    def test_a_helper_returning_what_it_filled_carries_it(self) -> None:
        """The other side of the row above: a helper whose RETURN is computed from the buffer it
        filled (`return f[3 * i]`) does carry the flux through what it returns."""
        kernel = (self._PW2_HELPER.format(ret="double", returned="f[3 * i]")
                  + "__global__ void faces(const double* u, double* f, double g, long n,"
                  " int* bad) {\n  const long i = threadIdx.x;\n"
                  "  if (i < n) { bad[i] = face_flux(u, f, g, i) > 0.0 ? 0 : 1; }\n}\n")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dep_model.cuh").write_text(self._PW2_HEADER)
            self.assertEqual([], self._pw2(tmp, kernel, "0.0"))

    def test_what_a_dependency_returns_is_not_what_it_fills(self) -> None:
        """The same for a dependency operation that RETURNS a guard while it fills an output:
        what it returns is computed from its inputs (its declaration says which are inputs), so
        `bad[i] = dep_ok(u[i], c) ? 0 : 1` does not make the flag stand for `c`."""
        header = self._PW2_HEADER.replace(
            "}\n", "__host__ __device__ bool dep__ok(\n    double h,\n    double& c);\n}\n")
        kernel = ("__global__ void faces(const double* u, double* f, double g, long n,"
                  " int* bad) {\n  const long i = threadIdx.x;\n  if (i < n) {\n"
                  "    double c = 0.0;\n    bad[i] = dep_model::dep__ok(u[i] * g, c) ? 0 : 1;\n"
                  "    f[i] = c;\n  }\n}\n")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dep_model.cuh").write_text(header)
            self.assertEqual([], self._pw2(tmp, kernel, "f[i]"))
            out = self._pw2(tmp, kernel, "0.0")
            self.assertEqual(1, len(out), out)
            self.assertIn("(candidates=['df'])", out[0])

    def test_a_stride_however_spelled_hands_over_the_buffer(self) -> None:
        """`View<double, 1>{f + kComp * k, {3}}` hands over `f`: what a dependency call writes
        is the declared STORAGE its actual names, whatever the stride is spelled with — a file
        constant (the shallow-water models' `constexpr int kComp = 3;`), an `auto` index, an
        integer parameter. Before, every plain name but an integer was read, so a file constant
        became a "result" and a guard flag computed from the inputs `u + kComp * k` stood for
        the dropped flux."""
        strides = {
            "file constant": ("constexpr int kComp = 3;\n", "  const long k = threadIdx.x;\n",
                              "kComp * k"),
            "auto index": ("", "  const auto k = threadIdx.x;\n", "3 * k"),
            "integer parameter": ("", "  const long k = threadIdx.x + n0;\n", "3 * k"),
        }
        for label, (prelude, index, stride) in strides.items():
            kernel = (prelude + "__global__ void faces(const double* u, double* f, double g,"
                      " long n, int* bad, long n0 = 0) {\n" + index
                      + "  if (k < n) {\n    double gy[3];\n    bool gok = true;\n"
                      f"    dep_model::dep__face2(atmofab::View<const double, 1>{{u + {stride},"
                      " {3}}, g,\n"
                      f"        atmofab::View<double, 1>{{f + {stride}, {{3}}}},"
                      " atmofab::View<double, 1>{gy, {3}}, gok);\n"
                      "    bad[k] = gok ? 0 : 1;\n  }\n}\n")
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "dep_model.cuh").write_text(self._PW2_HEADER)
                self.assertEqual([], self._pw2(tmp, kernel, "f[i]"))
                out = self._pw2(tmp, kernel, "0.0")
                self.assertEqual(1, len(out), out)
                self.assertIn("(candidates=['df'])", out[0])

    def test_a_result_written_to_shared_memory_and_dropped_is_refused(self) -> None:
        """A kernel that writes the flux into `__shared__` storage it never reads: the index
        `t` in `sf + 3 * t` no longer stands for the result (before: `t` reached the output
        through the thread index and the dropped flux passed)."""
        kernel = ("__global__ void faces(const double* u, double* f, double g, long n, int* bad)"
                  " {\n  __shared__ double sf[96];\n  const int t = threadIdx.x;\n"
                  "  const long q = blockIdx.x * blockDim.x + t;\n  double gy[3];\n"
                  "  bool gok = true;\n"
                  "  dep_model::dep__face2(atmofab::View<const double, 1>{u + 3 * q, {3}}, g,\n"
                  "      atmofab::View<double, 1>{sf + 3 * t, {3}},"
                  " atmofab::View<double, 1>{gy, {3}}, gok);\n"
                  "  f[q] = u[q];\n  bad[q] = gok ? 0 : 1;\n}\n")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dep_model.cuh").write_text(self._PW2_HEADER)
            out = self._pw2(tmp, kernel, "f[i]")
            self.assertTrue(any("function faces does not propagate" in v and "'sf'" in v
                                for v in out), out)

    def test_a_call_writing_an_output_parameter_needs_nothing_more(self) -> None:
        """Legitimate per-face kernels the binding invites: `F_star` straight into the output
        `f` (`&f[3 * q]`, or a pointer `double* fq = f + 3 * q;` into it) and `G_star` into a
        local the kernel does not need. Before: refused on the local alone, the output parameter
        being dropped from the candidates and the pointer not read as pointing into `f`. Each
        still refuses a caller that drops `f`."""
        spellings = {
            "address": ("", "atmofab::View<double, 1>{&f[3 * q], {3}}"),
            "pointer": ("    double* fq = f + 3 * q;\n", "atmofab::View<double, 1>{fq, {3}}"),
        }
        for label, (pointer, view) in spellings.items():
            kernel = ("__global__ void faces(const double* u, double* f, double g, long n,"
                      " int* bad) {\n  const long q = threadIdx.x;\n  if (q < n) {\n"
                      + pointer + "    double go[3];\n    bool gok = true;\n"
                      "    dep_model::dep__face2(atmofab::View<const double, 1>{u + 3 * q, {3}},"
                      f" g,\n        {view}, atmofab::View<double, 1>{{go, {{3}}}}, gok);\n"
                      "    bad[q] = gok ? 0 : 1;\n  }\n}\n")
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "dep_model.cuh").write_text(self._PW2_HEADER)
                self.assertEqual([], self._pw2(tmp, kernel, "f[i]"))
                out = self._pw2(tmp, kernel, "0.0")
                self.assertEqual(1, len(out), out)
                self.assertIn("(candidates=['df'])", out[0])

    def test_two_functions_of_one_name_do_not_stall_the_gate(self) -> None:
        """Codex, round 2 of #380 PR-1's review: `a::helper` reaching the dependency and
        `b::helper` not alternated `reached['helper']` forever, and two `copy`s with different
        summaries alternate the summaries' fixed point the same way. Keyed by name and only
        widened, both iterations end; bounded here by an alarm so a regression fails instead of
        hanging."""
        import signal

        body = ("namespace a { void helper(double& out) { dep_model::dep__speed(1.0, out); } }\n"
                "namespace b { void helper(int& out) { out += 1; } }\n"
                # Two of one name whose SUMMARIES differ (`y` from `x`, or from nothing): the
                # summaries' fixed point is keyed the same way and must end the same way.
                "namespace a { void copy(double x, double& y) { y = x; } }\n"
                "namespace b { void copy(double x, double& y) { (void)x; y += 1.0; } }\n"
                # ...and two whose RETURN summaries differ.
                "namespace a { double r(double x) { return x; } }\n"
                "namespace b { double r(double x) { (void)x; return 1.0; } }\n"
                "void p__f(double& x) { a::helper(x); int k = 0; b::helper(k);\n"
                "  double z = 0.0; a::copy(x, z); b::copy(x, z); x = a::r(z) + b::r(z); }")

        def alarm(_signum, _frame):
            raise TimeoutError("the dataflow gate did not return")
        previous = signal.signal(signal.SIGALRM, alarm)
        try:
            signal.alarm(10)
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "dep_model.cuh").write_text(self._PW2_HEADER)
                self.assertEqual([], self._gates(self._model(tmp, body, header=False), ["dep"]))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    def test_the_storage_and_integer_readers(self) -> None:
        """The two readers `_handed_over` rests on, one row per spelling: what a function
        declares as STORAGE, what it declares as an INTEGER, and the fallback to plain names
        minus those when an actual names no storage (a member, `w.buf + 3 * i`)."""
        fn = cpp_decls.read(
            "__global__ void k(const double* u, double* __restrict__ f, long n, int* bad,"
            " atmofab::View<double, 1> v, std::vector<double>& w, int base, const int t) {\n"
            "  __shared__ double sf[96];\n  double gy[3];\n  double* fq = f + 3 * n;\n"
            "  std::vector<double> fh(12);\n  atmofab::View<double, 1> fv{fh.data(), {3}};\n"
            "  const long i = 0;\n  std::size_t o = 3;\n  for (int j = 0; j < 3; ++j) {}\n"
            "  unsigned long m = 0;\n  std::int64_t big = 0;\n  double a = 1.0;\n"
            "  double b = a * n;\n  if (n) { return; }\n}\n").functions[0]
        self.assertEqual({"u", "f", "bad", "v", "w", "sf", "gy", "fq", "fh", "fv"},
                         cpp_source._storage_names_of(fn))
        self.assertEqual({"n", "base", "t", "i", "o", "j", "m", "big"},
                         cpp_source._integer_names(fn))
        # `return w[0];` reads like `<type> <name>[` and declares nothing.
        h = cpp_decls.read("double h(Work w) { return w[0]; }\n").functions[0]
        self.assertEqual(set(), cpp_source._storage_names_of(h))
        self.assertEqual({"f"}, cpp_source._handed_over("f + kComp * i", {"f"}, set()))
        self.assertEqual({"w"}, cpp_source._handed_over("w.buf + 3 * i", {"f"}, {"i"}))

    def test_a_call_to_a_function_that_calls_no_dependency_is_not_a_dependency_call(self) -> None:
        """Only a callee that reaches a dependency operation stands for one: a kernel that
        computes on its own is followed as before and asked nothing of — here it fills a scratch
        buffer nothing reads, which would be refused as a discarded result if it were asked."""
        body = ("__global__ void zero(double* f, long n) {\n"
                "  const long i = threadIdx.x;\n  if (i < n) { f[i] = 0.0; }\n}\n"
                + self._PW_FACES + self._PW_UPDATE.format(use="f[i]")
                + self._PW_STEP.format(faces="faces<<<1, 4>>>").replace(
                    "  upd<<<", "  double* ds = nullptr;\n  zero<<<1, 4>>>(ds, 4);\n  upd<<<"))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], self._gates(self._model(tmp, body), ["dep"]))

    def test_kernel_spellings_the_rules_invite(self) -> None:
        """Round 5 of this change's review, each through the REAL declaration reader: inputs
        `const double* __restrict__` (the reader dropped the element's `const`, making them
        outputs), an input `double*` the kernel only reads, and a grid-stride loop (its step
        `i += stride)` swallowed the body). Each passes consuming the flux and is refused
        discarding it."""
        kernels = {
            "const restrict inputs": (
                "__global__ void upd(const double* __restrict__ u, const double* __restrict__ f,"
                " double* __restrict__ out, double dt) {{\n  const int i = threadIdx.x;\n"
                "  out[i] = u[i] + dt * {use};\n}}\n"),
            "non-const input": (
                "__global__ void upd(const double* u, double* f, double* out, double dt) {{\n"
                "  const int i = threadIdx.x;\n  out[i] = u[i] + dt * {use};\n}}\n"),
            "grid-stride": (
                "__global__ void upd(const double* u, const double* f, double* out, double dt) {{\n"
                "  for (int i = threadIdx.x; i < 4; i += blockDim.x * gridDim.x) {{\n"
                "    out[i] = u[i] + dt * {use};\n  }}\n}}\n"),
            "grid-stride, no braces": (
                "__global__ void upd(const double* u, const double* f, double* out, double dt) {{\n"
                "  for (int i = threadIdx.x; i < 4; i += blockDim.x * gridDim.x)\n"
                "    out[i] = u[i] + dt * {use};\n}}\n"),
        }
        call = self._ROUND_TRIP.format(copied="flux", count="32", launch="upd<<<1, 4>>>")
        for label, kernel in kernels.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual([], self._gates(self._model(tmp, kernel.format(use="f[i]") + call),
                                                 ["dep"]))
                out = self._gates(self._model(tmp, kernel.format(use="u[i]") + call), ["dep"])
                self.assertTrue(any("does not propagate" in v for v in out), out)

    def test_a_loop_step_does_not_carry_a_discarded_result_to_the_output(self) -> None:
        """Round 5: `i += 1)` swallowed `finite = finite && isfinite(flux[i])`, making `i` take
        `flux`, and the index carried the discarded result to the output."""
        body = ("void p__step(atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,"
                " double dt) {\n  std::vector<double> flux(4);\n"
                "  dep_model::dep__flux(u, atmofab::View<double, 1>{flux.data(), {4}}, dt);\n"
                "  bool finite = true;\n"
                "  for (long i = 0; i < 4; i += 1) { finite = finite && flux[i] == flux[i]; }\n"
                "  for (long i = 0; i < 4; ++i) { u_new.data[i] = u.data[i]; }\n}")
        with tempfile.TemporaryDirectory() as tmp:
            out = self._gates(self._model(tmp, body), ["dep"])
            self.assertTrue(any("candidates=['flux']" in v for v in out), out)

    def test_a_qualified_helper_is_followed_and_summarized_from_its_body(self) -> None:
        helper = ("namespace detail {{\nvoid axpy(double* y, const double* x, const double* f,"
                  " double a, long n) {{\n  for (long i = 0; i < n; ++i) {{ y[i] = x[i] + a * {use}; }}"
                  "\n}}\n}}\n")
        call = ("void p__step(atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,"
                " double dt) {\n  std::vector<double> flux(4);\n"
                "  dep_model::dep__flux(u, atmofab::View<double, 1>{flux.data(), {4}}, dt);\n"
                "  detail::axpy(u_new.data, u.data, flux.data(), dt, 4);\n}")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], self._gates(self._model(tmp, helper.format(use="f[i]") + call),
                                             ["dep"]))
            out = self._gates(self._model(tmp, helper.format(use="x[i]") + call), ["dep"])
            self.assertTrue(any("does not propagate" in v for v in out), out)

    def test_a_view_s_extent_is_not_a_candidate(self) -> None:
        """Round 3 of this change's review: `View<...>{fp, {n}}` made `n` a candidate, and `n`
        reached the output through an index, passing a discarded flux. `n` is DECLARED with its
        initializer, so the "assigned before the call" clause does not drop it — round 4 found
        an earlier version of this row assigning it by a statement, which made the row pass
        with the fix disabled."""
        body = ("void p__step(atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,"
                " double dt) {\n  std::vector<double> flux(4);\n  long n = u.extent[0];\n"
                "  double* fp = flux.data();\n"
                "  dep_model::dep__flux(u, atmofab::View<double, 1>{fp, {n}}, dt);\n"
                "  for (long i = 0; i < 4; ++i) {\n    long j;\n    j = (i + 1) % n;\n"
                "    u_new.data[i] = 0.5 * (u.data[i] + u.data[j]);\n  }\n}")
        with tempfile.TemporaryDirectory() as tmp:
            out = self._gates(self._model(tmp, body), ["dep"])
            self.assertTrue(any("candidates=['fp']" in v for v in out), out)

    def test_each_dependency_call_s_result_must_reach_an_output(self) -> None:
        """Round 3 of this change's review: the candidates of every call were pooled, so one
        consumed result passed a second, discarded one — against the authoring rules."""
        header = ("namespace dep_model {\nvoid dep__flux(atmofab::View<const double, 1> u, "
                  "atmofab::View<double, 1> f, double dt);\nvoid dep__norm("
                  "atmofab::View<const double, 1> u, double& m);\n}\n")
        body = ("void p__step(atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,"
                " double& m_out, double dt) {\n  std::vector<double> flux(4);\n  double m;\n"
                "  dep_model::dep__flux(u, atmofab::View<double, 1>{flux.data(), {4}}, dt);\n"
                "  dep_model::dep__norm(u, m);\n  m_out = m;\n"
                "  for (long i = 0; i < 4; ++i) {\n    u_new.data[i] = u.data[i]{use};\n  }\n}")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dep_model.cuh").write_text(header)
            model = self._model(tmp, body.replace("{use}", ""), header=False)
            self.assertEqual([f"{model}: function p__step does not propagate dependency "
                              "operation outputs to its output dataflow (candidates=['flux'])"],
                             self._gates(model, ["dep"]))
            model = self._model(tmp, body.replace("{use}", " + dt * flux[static_cast<std::size_t>(i)]"),
                                header=False)
            self.assertEqual([], self._gates(model, ["dep"]))

    def test_a_guard_flag_does_not_stand_for_an_array_result(self) -> None:
        """Round 5 of this change's review: every advdiff / shallow-water dependency publishes a
        guard flag, and `ok = guard;` alone made the call \"propagated\" while its flux was
        discarded. An operation with an array output needs an array result to reach an output."""
        header = ("namespace dep_model {\nvoid dep__flux(atmofab::View<const double, 1> u, "
                  "atmofab::View<double, 1> f, double dt, bool& guard_pass);\n}\n")
        body = ("void p__step(atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,"
                " double dt, bool& ok) {\n  std::vector<double> flux(4);\n  bool guard;\n"
                "  dep_model::dep__flux(u, atmofab::View<double, 1>{flux.data(), {4}}, dt, guard);\n"
                "  ok = guard;\n"
                "  for (long i = 0; i < 4; ++i) {\n    u_new.data[i] = u.data[i]{use};\n  }\n}")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dep_model.cuh").write_text(header)
            model = self._model(tmp, body.replace("{use}", ""), header=False)
            self.assertEqual([f"{model}: function p__step does not propagate dependency "
                              "operation outputs to its output dataflow (candidates=['flux'])"],
                             self._gates(model, ["dep"]))
            model = self._model(tmp, body.replace("{use}", " + dt * flux[static_cast<std::size_t>(i)]"),
                                header=False)
            self.assertEqual([], self._gates(model, ["dep"]))

    def test_a_pointer_taken_by_address_aliases_the_storage(self) -> None:
        shape = ("  double* fp = &flux[0];\n"
                 "  dep_model::dep__flux(u, atmofab::View<double, 1>{fp, {4}}, dt);\n")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], self._gates(self._flux_model(tmp, shape, self._CONSUME), ["dep"]))
            out = self._gates(self._flux_model(tmp, shape, self._DISCARD), ["dep"])
            self.assertTrue(any("does not propagate" in v for v in out), out)

    def test_output_parameter_qualifiers(self) -> None:
        for ctype in ("double*__restrict__", "atmofab::View<double,1>const", "double*const"):
            self.assertTrue(cpp_source.is_output_parameter(ctype), ctype)
        for ctype in ("double const&", "const double*__restrict__"):
            self.assertFalse(cpp_source.is_output_parameter(ctype), ctype)

    def test_an_address_of_actual_and_an_assignment_after_a_keyword(self) -> None:
        """`&m` hands the storage of `m` over; after `else`, `do` or a ternary `?` an assignment
        is a STATEMENT, not a declaration's initializer (each clause of `_is_declaration`)."""
        header = ("namespace dep_model {\nvoid dep__norm(atmofab::View<const double, 1> u, "
                  "double& m);\n}\n")
        base = ("void p__run(atmofab::View<const double, 1> u, double& out, bool c) {\n"
                "  double m;\n{pre}  dep_model::dep__norm(u, {arg});\n  out = c ? 1.0 : 0.0;\n}")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dep_model.cuh").write_text(header)
            for arg in ("m", "&m"):
                model = self._model(tmp, base.replace("{pre}", "").replace("{arg}", arg),
                                    header=False)
                (Path(tmp) / "dep_model.cuh").write_text(header)
                self.assertTrue(any("candidates=['m']" in v for v in self._gates(model, ["dep"])),
                                arg)
            for pre in ("  if (c) { m = 1.0; } else m = 0.0;\n", "  do m = 0.0; while (false);\n",
                        "  c ? m = 1.0 : m = 0.0;\n"):
                model = self._model(tmp, base.replace("{pre}", pre).replace("{arg}", "m"),
                                    header=False)
                self.assertEqual([], self._gates(model, ["dep"]), pre)

    def test_a_value_returning_function_always_has_an_output(self) -> None:
        """Round 1 of this change's review: a function returning a literal had no output the
        gate could see, so it was skipped and its discarded dependency result passed."""
        body = ("double p__run(atmofab::View<const double, 1> u) {\n"
                "  std::vector<double> flux(4);\n"
                "  dep_model::dep__flux(u, atmofab::View<double, 1>{flux.data(), {4}}, 0.1);\n"
                "  return 1.0;\n}")
        with tempfile.TemporaryDirectory() as tmp:
            model = self._model(tmp, body)
            self.assertEqual([f"{model}: function p__run does not propagate dependency "
                              "operation outputs to its output dataflow (candidates=['flux'])"],
                             self._gates(model, ["dep"]))
            fixed = body.replace("return 1.0;", "return flux[0];")
            self.assertEqual([], self._gates(self._model(tmp, fixed), ["dep"]))

    def test_an_inert_call_with_every_actual_assigned_before_is_silent(self) -> None:
        """The neutral authoring rule 5: an inert dependency call assigns every actual before
        the call, which keeps this gate silent — at an output position of the header too."""
        body = ("void p__run(atmofab::View<const double, 1> u, double& out, double dt) {\n"
                "  std::vector<double> work(4);\n"
                "  for (int i = 0; i < 4; ++i) { work[static_cast<std::size_t>(i)] = 0.0; }\n"
                "  dep_model::dep__flux(u, atmofab::View<double, 1>{work.data(), {4}}, dt);\n"
                "  out = dt;\n}")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], self._gates(self._model(tmp, body), ["dep"]))
            written = body.replace(
                "  for (int i = 0; i < 4; ++i) { work[static_cast<std::size_t>(i)] = 0.0; }\n",
                "")
            model = self._model(tmp, written)
            self.assertEqual([f"{model}: function p__run does not propagate dependency "
                              "operation outputs to its output dataflow (candidates=['work'])"],
                             self._gates(model, ["dep"]))

    def test_an_input_position_of_the_header_is_not_a_candidate(self) -> None:
        """`dep__norm` READS its argument (a const reference in the header), so handing it an
        unassigned local discards nothing; without the header the same call is read as the
        Fortran binding reads it, every actual a candidate."""
        body = ("void p__total(double& out, double x) {\n"
                "  std::vector<double> scratch(3);\n"
                "  dep_model::dep__norm(scratch);\n"
                "  out = x;\n}")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], self._gates(self._model(tmp, body), ["dep"]))
            model = self._model(tmp, body, header=False)
            (Path(tmp) / "dep_model.cuh").unlink()
            self.assertEqual([f"{model}: function p__total does not propagate dependency "
                              "operation outputs to its output dataflow "
                              "(candidates=['scratch'])"], self._gates(model, ["dep"]))

    def test_without_the_header_every_actual_is_a_candidate_but_four_kinds(self) -> None:
        """The Fortran binding's candidate rule, where no dependency header says which
        parameter is written: a parameter, a `const` name, a function this file defines, and a
        name assigned before the call are not candidates."""
        body = ("const double k = 2.0;\n"
                "void helper() {}\n"
                "void p__run(double& out, double x) {\n"
                "  double pre;\n"
                "  pre = x;\n"
                "  double scratch;\n"
                "  dep_model::dep__apply(x, k, helper, pre, scratch);\n"
                "  out = pre;\n}")
        with tempfile.TemporaryDirectory() as tmp:
            model = self._model(tmp, body, header=False)
            self.assertEqual([f"{model}: function p__run does not propagate dependency "
                              "operation outputs to its output dataflow "
                              "(candidates=['scratch'])"], self._gates(model, ["dep"]))
            fixed = body.replace("out = pre;", "out = pre + scratch;")
            self.assertEqual([], self._gates(self._model(tmp, fixed, header=False), ["dep"]))

    def test_a_returned_value_is_an_output(self) -> None:
        body = ("double p__total(const std::vector<double>& u) {\n"
                "  double norm = dep_model::dep__norm(u);\n"
                "  double scratch;\n"
                "  dep_model::dep__other(scratch);\n"
                "  return norm + scratch;\n}")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], self._gates(self._model(tmp, body, header=False), ["dep"]))

    def test_a_metric_only_scalar_kernel_is_refused_on_a_multidimensional_node(self) -> None:
        body = ("void p__m(double x, double& a, double& b, double& c, double& d, double& e) {\n"
                "  a = x; b = x; c = x; d = x; e = x;\n}")
        with tempfile.TemporaryDirectory() as tmp:
            model = self._model(tmp, body)
            self.assertEqual([], self._gates(model, []))
            self.assertEqual([f"{model}: function p__m is metric-only scalar kernel for p; "
                              "2d/3d problem model must not derive many outputs without array "
                              "inputs or update loops"], self._gates(model, [], multidim="p"))
            looped = body.replace("a = x;", "for (int i = 0; i < 2; ++i) { a = x; }")
            self.assertEqual([], self._gates(self._model(tmp, looped), [], multidim="p"))
            viewed = body.replace("double x,", "atmofab::View<const double, 2> x,").replace(
                "= x;", "= x.data[0];")
            self.assertEqual([], self._gates(self._model(tmp, viewed), [], multidim="p"))

    def test_a_returned_value_counts_toward_the_metric_only_threshold(self) -> None:
        body = ("double p__m(double x, double& a, double& b, double& c, double& d) {\n"
                "  a = x; b = x; c = x; d = x;\n  return x;\n}")
        with tempfile.TemporaryDirectory() as tmp:
            out = self._gates(self._model(tmp, body), [], multidim="p")
            self.assertTrue(any("metric-only scalar kernel" in v for v in out), out)
            void = body.replace("double p__m", "void p__m").replace("  return x;\n", "")
            self.assertEqual([], self._gates(self._model(tmp, void), [], multidim="p"))

    def test_an_unreadable_model_is_refused_by_the_dependency_use_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "p_model.cu"
            model.write_text('#include "dep_model.cuh"\nnamespace p_model {\n'
                             "void p__run(double& x) { x = dep_model::dep__norm({}); }\n")
            out: list[str] = []
            cpp_source.validate_dependency_operations([model], ["dep"], out)
            self.assertEqual(1, len(out), out)
            self.assertIn("the dependency-use gate cannot read this source's declarations", out[0])

    def test_an_unreadable_problem_model_is_refused_not_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "p_model.cu"
            model.write_text("namespace p_model {\nvoid p__lit(double& a) { a = 1.0; }\n")
            out = self._gates(model, [])
            self.assertTrue(any("cannot read this source's declarations" in v for v in out), out)

    def test_output_parameter_classification(self) -> None:
        outs = ["double&", "std::vector<double>&", "atmofab::View<double,1>", "View<double,2>",
                "double*", "atmofab::Array<double,2>&"]
        ins = ["double", "const double&", "atmofab::View<const double,1>", "const double*",
               "const std::vector<double>&", "int"]
        for ctype in outs:
            self.assertTrue(cpp_source.is_output_parameter(ctype), ctype)
        for ctype in ins:
            self.assertFalse(cpp_source.is_output_parameter(ctype), ctype)


class ToolAdapterTests(unittest.TestCase):
    def test_bundle_facts(self) -> None:
        self.assertEqual("s_model.cu", cpp_bundle.model_basename("s"))
        self.assertEqual("s_runner.cu", cpp_bundle.runner_basename("s"))
        self.assertEqual("nvcc", cpp_bundle.MANDATORY_SYNTAX_COMPILER)
        self.assertEqual(cpp_bundle.DEFAULT_COMPILER, cpp_bundle.MANDATORY_SYNTAX_COMPILER)
        self.assertTrue(re.fullmatch(cpp_bundle.IDENTIFIER_PATTERN, "a" * 1024))
        self.assertIsNone(re.fullmatch(cpp_bundle.IDENTIFIER_PATTERN, "a" * 1025))

    def test_syntax_adapter_argv(self) -> None:
        self.assertEqual(
            ["nvcc", "-std=c++17", "-arch=sm_90", "-Xcompiler", "-fsyntax-only", "-odir", ".mods",
             "-rdc=true", "-c", "a.cu"],
            nvcc_syntax.argv(standard="c++17", scratch_dir=".mods", openmp=True, promotions=(),
                             architecture="sm_90", sources=["a.cu"]))
        self.assertNotIn("-arch=None", nvcc_syntax.argv(
            standard="c++17", scratch_dir=".m", openmp=False, promotions=(), architecture=None,
            sources=[]))
        self.assertEqual("cuda_cpp", nvcc_syntax.LANGUAGE)
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("b.cu", "a.cu", "c.txt"):
                (Path(tmp) / name).write_text("")
            self.assertEqual(["a.cu", "b.cu"], cpp_syntax.compile_order(Path(tmp)))

    def test_relocatable_device_code_is_in_every_argv_that_compiles_device_code(self) -> None:
        """Issue #380: the build, the syntax stage and the lint all compile a kernel's call to a
        `__host__ __device__` operation of another file, which only relocatable device code
        resolves (the real-driver row below measures why)."""
        from tools.backends.language.cuda_cpp import control_file as cpp_control_file
        self.assertEqual(1, cpp_control_file.flags("c++17", "sm_90").split().count("-rdc=true"))
        self.assertEqual(1, cpp_control_file.flags("c++17", None).split().count("-rdc=true"))
        self.assertIn("-rdc=true", cpp_control_file.rules(
            standard="c++17", parallel_backend="cuda")["flags"].split())
        self.assertIn("-rdc=true", nvcc_syntax.argv(
            standard="c++17", scratch_dir=".m", openmp=False, promotions=(), architecture=None,
            sources=["a.cu"]))
        self.assertIn("-rdc=true", nvcc_lint.CHECK_FLAGS)
        self.assertIn("-rdc=true", nvcc_lint.self_check_argv("/x"))

    def test_linter_declaration(self) -> None:
        self.assertEqual(("nvcc", *nvcc_lint.CHECK_FLAGS), nvcc_lint.check_argv())
        self.assertEqual((*nvcc_lint.check_argv(), "./a.cu"), nvcc_lint.source_argv(["./a.cu"]))
        banner = ("nvcc: NVIDIA (R) Cuda compiler driver\nCopyright (c) 2005-2026 NVIDIA\n"
                  "Cuda compilation tools, release 13.4, V13.4.92\n")
        self.assertEqual((13, 4, 92), nvcc_lint.parse_version(banner))
        # Only the `release` line is read: a dotted number earlier in the output is not the build.
        self.assertEqual((13, 4, 92), nvcc_lint.parse_version("driver 2.0\n" + banner))
        self.assertIsNone(nvcc_lint.unsupported_version_reason(banner))
        self.assertIsNotNone(nvcc_lint.unsupported_version_reason(banner.replace("13.4", "12.9")))
        self.assertIsNotNone(nvcc_lint.unsupported_version_reason(banner.replace("13.4", "14.0")))
        self.assertIsNotNone(nvcc_lint.unsupported_version_reason("Copyright (c) 2005-2026"))
        for rc in (0, 1, 2):
            self.assertIsNone(nvcc_lint.unusable_invocation_reason(rc, "", ""))
        self.assertIsNone(nvcc_lint.unusable_invocation_reason(255, "", ""))  # a ptxas error
        self.assertIsNotNone(nvcc_lint.unusable_invocation_reason(3, "", ""))
        self.assertEqual(("-x", "cu", "/dev/null"), nvcc_lint.self_check_argv("/x")[-3:])
        self.assertIsNone(nvcc_lint.self_check_reason(0, "", ""))
        self.assertIsNotNone(nvcc_lint.self_check_reason(1, "", ""))
        document = nvcc_lint.lint_rules_document()
        self.assertIn(" ".join(nvcc_lint.check_argv()), document)

    def test_parallel_backend(self) -> None:
        self.assertEqual({}, cuda_execution.environment(1))
        for bad in (0, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cuda_execution.environment(bad)
        floor = cuda_directives.presence_floor(language="cuda_cpp", hardware_class="gpu")
        self.assertIsNone(cuda_directives.presence_floor(language="cuda_cpp", hardware_class="cpu"))
        self.assertIsNone(cuda_directives.presence_floor(language="fortran", hardware_class="gpu"))
        self.assertTrue(floor.directive.search("__global__ void k() {}"))
        self.assertTrue(floor.directive.search("k<<<1, 2>>>();"))
        self.assertFalse(floor.directive.search("// __global__ and <<< in a comment\n"
                                                "const char* s = \"<<<\";\n"))
        self.assertIn("counted `for` loop", floor.remedy(Path("m.cu"), 3))
        self.assertTrue(cuda_directives.lowering_plan_declines(
            {"parallelization": {"model": "none"}}))
        self.assertFalse(cuda_directives.lowering_plan_declines(
            {"parallelization": {"model": "cuda"}}))
        self.assertFalse(cuda_directives.lowering_plan_declines({}))

    def test_the_leaf_is_told_the_device_callable_binding(self) -> None:
        """Issue #380 (B1): what a `cuda_cpp` leaf reads about a pointwise operation agrees with
        what the header and the pin do — the producer's rule 6a, the reviewer's dependency-call
        item, the checks-ABI binding's §5 and the lint flags it is shown. A literal guard on the
        removed phrasing ("a HOST function: no `__device__`") as much as on the new one: a
        leaf told both would be told to define the operation in a way the pin refuses."""
        prompts = registry.capability_module("language", "cuda_cpp", "prompt_fragments")
        rule = prompts.fragments("generate_generate")["authoring_rule_6"]
        self.assertIn("define it with exactly that pair", rule)
        self.assertIn("`__host__ __device__`", rule)
        self.assertNotIn("A published operation is a HOST function", rule)
        verify = prompts.fragments("generate_verify")["dependency_call_consistency"]
        self.assertIn("`__host__ __device__` may be called from inside a kernel", verify)
        section5 = registry.capability_module("language", "cuda_cpp", "checks_abi").document()
        section5 = section5[section5.index("## 5. "):]
        self.assertIn("`__host__ __device__` as its header declares", section5)
        self.assertNotIn("Published operations are host functions", section5)
        self.assertNotIn("a kernel over the shared-memory limit)", section5)
        self.assertIn("-rdc=true", nvcc_lint.lint_rules_document())
        # Round 1 of #380 PR-1's review: what the producer and reviewer read beside the binding.
        # The lint argv rule (1) quotes is the declared one, derived from the code; the syntax
        # argv carries the build's relocatable device code; a pointwise operation's plan
        # declares `none`, and the reviewer reads that as the binding rather than a finding.
        rules = prompts.fragments("generate_generate")["authoring_rules_1_to_4"]
        self.assertIn(f"`{' '.join(nvcc_lint.check_argv())}`", rules)
        self.assertIn("-Xcompiler -fsyntax-only -rdc=true -c`", rules)
        self.assertIn("-Xcompiler -fsyntax-only -rdc=true -c`", section5)
        self.assertIn('declares `"model": "none"`', rule)
        g6 = prompts.fragments("generate_verify")["checklist_g6_floor_scope"]
        self.assertIn('its plan declares `"model": "none"`', g6)
        self.assertIn("that is the binding, not a finding", g6)

    def test_the_registry_serves_every_declared_capability(self) -> None:
        for capability in ("bundle_facts", "syntax_promotions", "prompt_fragments", "checks_abi",
                           "source_reading", "signatures"):
            with self.subTest(capability=capability):
                registry.capability_module("language", "cuda_cpp", capability)
        for capability in ("control_file", "interface_header", "runner_render"):
            with self.subTest(capability=capability):
                registry.capability_module("language", "cuda_cpp", capability)
        self.assertFalse(registry.provides("language", "fortran", "interface_header"))
        self.assertEqual("nvcc", registry.linter_for_language("cuda_cpp"))
        prompts = registry.capability_module("language", "cuda_cpp", "prompt_fragments")
        self.assertEqual({"interface_prototypes", "runner_import", "types_and_parameters",
                          "gate_guard_examples"},
                         set(prompts.fragments("generate_generate_harness")))
        self.assertEqual({"host_rendered_scope"}, set(prompts.fragments("generate_verify_harness")))
        # The physics templates' fragments (R4-b PR-6): the same marker set as the Fortran
        # files, so a template composed for either language resolves every marker.
        fortran = registry.capability_module("language", "fortran", "prompt_fragments")
        for template in ("generate_generate", "generate_verify"):
            with self.subTest(template=template):
                self.assertEqual(set(fortran.fragments(template)),
                                 set(prompts.fragments(template)))
        with self.assertRaises(ValueError):
            prompts.fragments("no_such_template")
        self.assertTrue(prompts.runner_output_document().startswith("# Runner output"))
        abi = registry.capability_module("language", "cuda_cpp", "checks_abi").document()
        self.assertIn("## 5. CUDA C++ legality and gate guards", abi)


class DeviceTraceTests(unittest.TestCase):
    """The `device_trace` capability (issue #307): what a binary runs under, what file the summary
    command writes, and the two readers the post_execute kernel gate uses."""

    #: The summary the `cpp_gpu` site's Nsight Systems 2025.1.3 wrote for a probe binary built
    #: `-arch=all` (issue #307 comment 5851969504), verbatim: a comma-bearing name is quoted, a
    #: template kernel has one row per instantiation, a namespaced one is qualified, and the
    #: kernel the probe defined and never launched (`never_k`) has no row.
    SITE_SUMMARY = (
        "Time (%),Total Time (ns),Instances,Avg (ns),Med (ns),Min (ns),Max (ns),StdDev (ns),"
        "Name\n"
        '51.4,3040,2,1520.0,1520.0,1312,1728,294.2,"plain_k(double *, long)"\n'
        "16.8,992,1,992.0,992.0,992,992,0.0,void tmpl_k<double>(T1 *)\n"
        "16.8,992,1,992.0,992.0,992,992,0.0,void tmpl_k<int>(T1 *)\n"
        "15.1,896,1,896.0,896.0,896,896,0.0,ns::ns_k(int *)\n")

    def test_the_profile_prefix_and_the_summary_command_name_the_stem(self) -> None:
        prefix = cuda_trace.profile_argv_prefix("stem_x")
        self.assertEqual(prefix[0], cuda_trace.EXECUTABLES[0])
        self.assertIn("stem_x", prefix)
        self.assertIn("--force-overwrite=true", prefix)
        summary = cuda_trace.summary_argv("stem_x")
        # The stats command proper follows the fresh-output wrapper (below).
        self.assertIn(cuda_trace.EXECUTABLES[0], summary)
        self.assertEqual(summary[summary.index(cuda_trace.EXECUTABLES[0]) + 1], "stats")
        # It reads the report the prefix wrote, and writes under the same stem.
        self.assertEqual(summary[-1], "stem_x.nsys-rep")
        self.assertEqual(summary[summary.index("-o") + 1], "stem_x")
        for flag in ("--force-export=true", "--force-overwrite=true"):
            self.assertIn(flag, summary)
        self.assertEqual(summary[summary.index("-f") + 1], "csv")

    def _run_summary(self, tmp: Path, fake_stats: str) -> subprocess.CompletedProcess:
        """Run the real `summary_argv` in `tmp` with a stand-in for the trace's program on PATH,
        whose body is `fake_stats` (a shell script)."""
        bindir = tmp / "bin"
        bindir.mkdir()
        fake = bindir / cuda_trace.EXECUTABLES[0]
        fake.write_text("#!/bin/sh\n" + fake_stats)
        fake.chmod(0o755)
        import os
        env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
        return subprocess.run(list(cuda_trace.summary_argv("kernel_trace")), cwd=tmp, env=env,
                              capture_output=True, text=True, check=False)

    #: The export database a readable report's stats run writes beside the summary.
    DB = "kernel_trace.sqlite"

    def test_the_summary_command_empties_its_path_before_the_stats_run(self) -> None:
        """Round 1: a READ-ONLY file at the summary's path survives `nsys stats
        --force-overwrite=true`, which then exits 0 (measured on 2026.3.2). The command removes
        whatever is there first, so a stats run that writes nothing leaves NO file (which the
        host refuses) rather than the binary's."""
        name = cuda_trace.summary_file("kernel_trace")
        self.assertIn(self.DB, cuda_trace.summary_argv("kernel_trace"))
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            forged = tmp / name
            forged.write_text("Instances,Name\n5,step_kernel(double *)\n")
            forged.chmod(0o444)
            # A stats run that exports the report but fails to write the summary, exiting 0 —
            # the measured shape.
            proc = self._run_summary(
                tmp, f'echo x > {self.DB}; echo "ERROR: Unable to open output file"\nexit 0\n')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(forged.exists())
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            # And the stats run is what writes it, with its own argv after the wrapper's.
            proc = self._run_summary(
                tmp, f'printf "%s\\n" "$@" > argv.txt; printf "Instances,Name\\n" > {name}; '
                     f'echo x > {self.DB}\n')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((tmp / name).read_text(), "Instances,Name\n")
            argv = (tmp / "argv.txt").read_text().splitlines()
            full = cuda_trace.summary_argv("kernel_trace")
            self.assertEqual(argv, list(full[full.index(cuda_trace.EXECUTABLES[0]) + 1:]))

    def test_a_stats_run_that_exports_nothing_fails_the_command(self) -> None:
        """Round 2: a report that is not readable (truncated, or other bytes) makes the stats
        run exit 0 with an EMPTY summary — the shape of "no kernel data" — and no export database
        (measured on 2026.3.2). The command fails when the database is absent after the run, and
        one the binary left beforehand is removed first, so it cannot stand in for the export.
        The stats run's own failure passes through with its code."""
        name = cuda_trace.summary_file("kernel_trace")
        for left_behind in (False, True):
            with self.subTest(left_behind=left_behind), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                if left_behind:
                    (tmp / self.DB).write_text("forged")
                    (tmp / self.DB).chmod(0o444)
                proc = self._run_summary(tmp, f": > {name}\nexit 0\n")
                self.assertEqual(proc.returncode, 3, proc.stderr)
                self.assertIn("was not exported", proc.stderr)
                self.assertFalse((tmp / self.DB).exists())
        with tempfile.TemporaryDirectory() as raw:
            proc = self._run_summary(Path(raw), "exit 7\n")
            self.assertEqual(proc.returncode, 7)
        with tempfile.TemporaryDirectory() as raw:
            proc = self._run_summary(Path(raw), f": > {name}; echo x > {self.DB}\nexit 0\n")
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_summary_path_that_cannot_be_emptied_fails_the_command(self) -> None:
        name = cuda_trace.summary_file("kernel_trace")
        import os
        # Root unlinks inside a read-only directory, so that shape is only a refusal for others.
        shapes = ("directory",) + (("read-only directory",) if os.geteuid() != 0 else ())
        for shape in shapes:
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                if shape == "directory":
                    (tmp / name).mkdir()
                else:
                    (tmp / name).write_text("Instances,Name\n5,k(int *)\n")
                ran = tmp / "stats_ran"
                bindir_holder = tmp / "holder"
                bindir_holder.mkdir()
                if shape == "read-only directory":
                    run = tmp / "run"
                    run.mkdir()
                    (tmp / name).rename(run / name)
                    run.chmod(0o555)
                    cwd = run
                else:
                    cwd = tmp
                import os
                fake = bindir_holder / cuda_trace.EXECUTABLES[0]
                fake.write_text(f"#!/bin/sh\ntouch {ran}\nexit 0\n")
                fake.chmod(0o755)
                env = {**os.environ,
                       "PATH": f"{bindir_holder}{os.pathsep}{os.environ['PATH']}"}
                try:
                    proc = subprocess.run(list(cuda_trace.summary_argv("kernel_trace")),
                                          cwd=cwd, env=env, capture_output=True, text=True,
                                          check=False)
                finally:
                    cwd.chmod(0o755)
                self.assertNotEqual(proc.returncode, 0)
                self.assertFalse(ran.exists(), "the stats run must not start")

    def test_the_summary_file_is_what_the_stats_command_writes(self) -> None:
        # Measured on both versions: `-o X` with this report writes `X_cuda_gpu_kern_sum.csv`.
        report = cuda_trace.summary_argv("s")[cuda_trace.summary_argv("s").index("-r") + 1]
        self.assertEqual(cuda_trace.summary_file("s"), f"s_{report}.csv")
        self.assertEqual(cuda_trace.summary_file("kernel_trace"),
                         "kernel_trace_cuda_gpu_kern_sum.csv")

    def test_an_empty_summary_is_no_kernel_data(self) -> None:
        for text in ("", "\n", "  \n\n"):
            with self.subTest(text=text):
                self.assertIsNone(cuda_trace.kernel_instances(text))
        header_only = self.SITE_SUMMARY.splitlines(keepends=True)[0]
        self.assertIsNone(cuda_trace.kernel_instances(header_only))

    def test_instances_are_summed_per_base_name(self) -> None:
        self.assertEqual(cuda_trace.kernel_instances(self.SITE_SUMMARY),
                         {"plain_k": 2, "tmpl_k": 2, "ns_k": 1})
        # The columns are read by NAME: a reordered header reads the same.
        rows = list(__import__("csv").reader(self.SITE_SUMMARY.splitlines()))
        reordered = "\n".join(",".join(f'"{c}"' for c in reversed(r)) for r in rows)
        self.assertEqual(cuda_trace.kernel_instances(reordered),
                         {"plain_k": 2, "tmpl_k": 2, "ns_k": 1})

    def test_base_kernel_name_strips_what_the_demangler_adds(self) -> None:
        for demangled, base in (("k(double *, long)", "k"), ("ns::k(int *)", "k"),
                                ("void k<double>(T1 *)", "k"),
                                ("void a::b::k<pair<int, int>>(T1 *, T2)", "k"),
                                ("(anonymous namespace)::k(int *)", "k"),
                                ("k", "k"), ("  k(void)  ", "k")):
            with self.subTest(demangled=demangled):
                self.assertEqual(cuda_trace.base_kernel_name(demangled), base)

    def test_a_summary_without_the_columns_is_unreadable(self) -> None:
        for text in ("Time (%),Total Time (ns),Count,Name\n1,2,3,k(int *)\n",
                     "Time (%),Instances,Kernel\n1,2,k(int *)\n",
                     "Instances,Name\nmany,k(int *)\n",
                     "Instances,Name\n-1,k(int *)\n",
                     "Instances,Name\n3,\n"):
            with self.subTest(text=text), self.assertRaises(cuda_trace.SummaryUnreadable):
                cuda_trace.kernel_instances(text)
        self.assertTrue(issubclass(cuda_trace.SummaryUnreadable, ValueError))

    def test_defined_kernels_reads_definitions_declarations_and_templates_over_code_only(
            self) -> None:
        src = (
            "// __global__ void in_comment(int*)\n"
            "/* __global__ void in_block(int*) */\n"
            'const char* s = "__global__ void in_string(int*)";\n'
            "__device__ double helper(double x) { return x; }\n"
            "__global__ void plain_k(double* x, long n) { x[0] = n; }\n"
            "static __global__ void static_k(int* x) {}\n"
            "template <class T> __global__ void tmpl_k(T* x);\n"
            "template <> __global__ void tmpl_k<double>(double* x) {}\n"
            "__global__ void __launch_bounds__(256) bounded_k(int* x) {}\n"
            "__global__ __attribute__((noinline)) void attr_k(int* x) {}\n"
            "__global__ void ns::qualified_k(int* x) {}\n"
            'extern "C" __global__ void c_k(void) {}\n'
            "__global__\nvoid\nsplit_k(\n  int* x) {}\n"
            "__global__ void plain_k(double* x, long n);\n")
        self.assertEqual(cuda_trace.defined_kernels(src),
                         ("plain_k", "static_k", "tmpl_k", "bounded_k", "attr_k",
                          "qualified_k", "c_k", "split_k"))
        self.assertEqual(cuda_trace.defined_kernels("__device__ int f(int x);\nint main() {}\n"),
                         ())
        # One construct per row, where the rows above would hide it: an explicit specialization
        # read alone (above, the primary template already named `tmpl_k`), the bracketed and
        # the other CUDA `__name__(…)` attributes, each of which carries a parenthesis.
        for text, name in (
                ("template <> __global__ void spec_k<double>(double* x) {}\n", "spec_k"),
                ('__global__ [[deprecated("old")]] void dep_k(int* x) {}\n', "dep_k"),
                ("__global__ void __cluster_dims__(2, 1, 1) cl_k(int* x) {}\n", "cl_k"),
                ("__global__ void __maxnreg__(32) reg_k(int* x) {}\n", "reg_k"),
                ("__global__ void __launch_bounds__(128, 2) lb_k(int* x) {}\n", "lb_k"),
                ("__global__ void __launch_bounds__((N), 2) nested_k(int* x) {}\n", "nested_k"),
                # A kernel whose own name is spelled like an attribute is a kernel.
                ("__global__ void __k__(int* x) {}\n", "__k__")):
            with self.subTest(text=text):
                self.assertEqual(cuda_trace.defined_kernels(text), (name,))

    def test_an_attribute_naming_global_marks_a_kernel(self) -> None:
        """nvcc makes a kernel of a function carrying the `global` attribute without the keyword
        (issue #307 PR-3 round 1; each spelling below measured on nvcc: an entry symbol in the
        object). Unread, such a kernel was required by nothing, so a producer told a kernel never
        ran could respell it and the gate would ask nothing of it. An attribute naming anything
        else is still blanked, `globals` included."""
        for attribute in ("[[gnu::global]]", "[[ gnu :: global ]]",
                          "[[gnu::global, gnu::noinline]]", "__attribute__((global))"):
            with self.subTest(attribute=attribute):
                self.assertEqual(cuda_trace.defined_kernels(
                    f"namespace m {{\n{attribute} void step_k(double* x, long n) {{}}\n}}\n"),
                    ("step_k",))
        for attribute in ("[[nodiscard]]", "[[gnu::globals]]", "__attribute__((noinline))",
                          "[[gnu::global_k]]"):
            with self.subTest(attribute=attribute):
                self.assertEqual(
                    cuda_trace.defined_kernels(f"{attribute} int host_f(int* x) {{}}\n"), ())

    def test_a_mark_anywhere_before_the_parameter_list_marks_its_declarator(self) -> None:
        """Round 2: nvcc makes a kernel of a function whose mark — the keyword or the attribute —
        stands after the return type or after the NAME, before the parameter list (each
        declaration below measured: an entry symbol in the object, or for the template one a
        clean compile), and of a parenthesized name. Reading the name forward from the mark read
        none of them; the name is read back from the parameter list."""
        for text in ("void k __global__ (int* p) {}\n",
                     "void __global__ k(int* p) {}\n",
                     "static void __global__ k(int* p) {}\n",
                     "void [[gnu::global]] k(int* p) {}\n",
                     "void k [[gnu::global]] (int* p) {}\n",
                     "auto k [[gnu::global]] (int* p) -> void {}\n",
                     "template <class T> void k [[gnu::global]] (T* p) {}\n",
                     "void __global__ (k)(int* p) {}\n",
                     "__global__ void (k)(int* p) {}\n",
                     "__global__ void __launch_bounds__(((256))) k(int* p) {}\n",
                     # An attribute whose argument carries its own brackets does not end early.
                     "[[gnu::global, gnu::aligned(alignof(int[1]))]] void k(int* p) {}\n"):
            with self.subTest(text=text):
                self.assertEqual(cuda_trace.defined_kernels(text), ("k",))

    def test_a_mark_with_no_parameter_list_in_its_declaration_names_nothing(self) -> None:
        """The declarator is looked for only up to the end of the mark's own declaration: a
        mark that reaches `;`, `{` or `}` before any parameter list names nothing, rather than
        the function the NEXT declaration declares (round 2: both halves of that stop were
        unpinned, and dropping either made `helper` a kernel below)."""
        for text in ("[[gnu::global]] int counter;\nvoid helper(int* x) {}\n",
                     "namespace n { [[gnu::global]] }\nvoid helper(int* x) {}\n",
                     "struct S { int a [[gnu::global]]; };\nvoid helper(int* x) {}\n"):
            with self.subTest(text=text):
                self.assertEqual(cuda_trace.defined_kernels(text), ())

    def test_an_attribute_argument_naming_global_is_not_a_mark(self) -> None:
        """Only the attribute's NAME decides: a helper aligned by a constant called `global` is
        not a kernel (round 2: reading every identifier in the attribute made it one, and a
        correct run would have been refused for a helper the trace never lists)."""
        text = ("constexpr int global = 16;\n"
                "[[gnu::aligned(global)]] __device__ void helper(int* x) {}\n"
                "__attribute__((aligned(global))) int buffer[4];\n"
                "__global__ void k(int* x) {}\n")
        self.assertEqual(cuda_trace.defined_kernels(text), ("k",))

    def test_the_registry_serves_the_trace(self) -> None:
        self.assertIs(registry.capability_module("parallel", "cuda", "device_trace"), cuda_trace)
        for value in ("openmp", "none"):
            with self.subTest(value=value):
                self.assertFalse(registry.provides("parallel", value, "device_trace"))


class SyntaxStagingTests(unittest.TestCase):
    def test_the_stage_holds_every_staged_file_at_its_relative_path(self) -> None:
        """Codex, round 2: a nested bundle file included from a top-level source must be staged
        where the include finds it, and the host header must be staged at all."""
        from tools.workflow_conductor import stage_syntax_inputs
        with tempfile.TemporaryDirectory() as tmp:
            src, stage = Path(tmp) / "src", Path(tmp) / "stage"
            (src / "detail").mkdir(parents=True)
            stage.mkdir()
            for name in ("h_model.cu", "h_model.cuh", "detail/helper.cu", "Makefile",
                         "command_log.jsonl"):
                (src / name).write_text("")
            (src / "link.cu").symlink_to(src / "h_model.cu")
            stage_syntax_inputs(src, stage, tuple(cpp_syntax.STAGED_SUFFIXES))
            self.assertEqual(["detail/helper.cu", "h_model.cu", "h_model.cuh"],
                             sorted(p.relative_to(stage).as_posix()
                                    for p in stage.rglob("*") if p.is_file()))
        header_suffix = Path(cpp_header.basename("h")).suffix
        self.assertIn(header_suffix, cpp_syntax.STAGED_SUFFIXES)
        self.assertNotIn(header_suffix, cpp_syntax.SOURCE_SUFFIXES)


class IdentifierBoundStatementTests(unittest.TestCase):
    def test_the_compile_contract_s_spec_id_bound_is_the_shortest_language_bound(self) -> None:
        """`phase_01_compile.md` states the spec_id bound keeps the derived identifiers within
        "63 characters, the shortest identifier bound an implemented language backend declares";
        with a second language that is a claim about the set, checked against the backends."""
        bounds = {language: registry.capability_module("language", language, "bundle_facts")
                  .IDENTIFIER_MAX
                  for language in registry.implemented_backend_ids("language")
                  if registry.provides("language", language, "bundle_facts")}
        self.assertGreaterEqual(len(bounds), 2)
        self.assertEqual(63, min(bounds.values()))
        text = (REPO_ROOT / "docs" / "workflow" / "phases" / "phase_01_compile.md").read_text(
            encoding="utf-8")
        self.assertIn(f"within {min(bounds.values())} characters, the shortest identifier bound "
                      "an implemented language backend declares", text)


class MakeGateTests(unittest.TestCase):
    """The make backend's two readings of a language that leaves no module artifact."""

    _MAKEFILE = (
        "OBJDIR ?= obj\nBINDIR ?= bin\nBIN ?= h_runner\nNVCC := nvcc\n"
        "$(BINDIR)/$(BIN): $(OBJDIR)/h_runner.o\n\t$(NVCC) -o $@ $^\n"
        "$(OBJDIR)/h_runner.o: h_runner.cu h_model.cu\n\t$(NVCC) -c $< -o $@\n")

    def test_a_source_prerequisite_is_not_read_as_a_module_artifact(self) -> None:
        from tools.backends.build_system.make import gates
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "h_model.cu").write_text("#pragma once\n")
            (src / "h_runner.cu").write_text('#include "h_model.cu"\nint main() { return 0; }\n')
            (src / "Makefile").write_text(self._MAKEFILE)
            out: list[str] = []
            gates.validate_src_dir(src, out, source_reading=cpp_source, language="cuda_cpp")
            self.assertEqual([], out)

    def test_with_no_artifact_an_edge_requires_the_object(self) -> None:
        from tools.backends.build_system.make import gates

        class Reader:
            MODULE_SOURCE_SUFFIXES = (".cu",)
            MODULE_ARTIFACT_SUFFIX = None

            @staticmethod
            def source_module_deps(files):
                return {"h_runner": {"h_model"}, "h_model": set()}

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "h_model.cu").write_text("")
            (src / "h_runner.cu").write_text("")
            (src / "Makefile").write_text(self._MAKEFILE)
            out: list[str] = []
            gates.validate_src_dir(src, out, source_reading=Reader, language="cuda_cpp")
            self.assertTrue(any("h_runner.o missing prerequisite for used module (h_model.o)" in v
                                for v in out), out)
            (src / "Makefile").write_text(self._MAKEFILE.replace(
                "h_runner.cu h_model.cu", "h_runner.cu $(OBJDIR)/h_model.o"))
            out = []
            gates.validate_src_dir(src, out, source_reading=Reader, language="cuda_cpp")
            self.assertEqual([], out)

    def test_the_cuda_compiler_variable_relinks(self) -> None:
        from tools.backends.build_system.make import gates
        self.assertTrue(gates._recipe_line_relinks("\t$(NVCC) -o $(BIN) main.o", {}))
        self.assertTrue(gates._recipe_line_relinks("\t${NVCC} -o $(BIN) main.o", {}))
        self.assertFalse(gates._recipe_line_relinks("\techo NVCC is the driver", {}))


@unittest.skipUnless(NVCC, "the CUDA compiler driver is not installed")
class RealDriverTests(unittest.TestCase):
    """Measured claims of `docs/backends/linter/nvcc/RULES.md` and the syntax adapter, run."""

    def _lint(self, source: str) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "x.cu").write_text(source)
            return subprocess.run(list(nvcc_lint.source_argv(["./x.cu"])), cwd=tmp,
                                  capture_output=True, check=False).returncode

    def test_lint_verdicts(self) -> None:
        self.assertEqual(0, self._lint(
            "__global__ void k(int n, double* y) { if (n) y[0] = 0; }\n"
            "void run(double* y) { k<<<1, 32>>>(1, y); }\n"))
        self.assertIn(self._lint("int g(int p) { return 2; }\n"), (1, 2))
        self.assertIn(self._lint("int f() { int v = 1; return 2; }\n"), (1, 2))
        self.assertEqual(0, self._lint("int g(int p) { (void)p; return 2; }\n"))

    def test_a_kernel_may_call_a_pointwise_operation_of_another_file(self) -> None:
        """Issue #380, measured with the repository's own argv builders: a consumer kernel calling
        a component's `__host__ __device__` operation (header rendered by the host) lints, passes
        the syntax stage, compiles with the build's flags and links; without `-rdc=true` the
        device code generator refuses the same source (`Unresolved extern function`)."""
        from tools.backends.language.cuda_cpp import control_file as cpp_control_file
        consumer = ('#include "c_model.cuh"\n'
                    "namespace {\n__global__ void faces(const double* u, double* f, long n) {\n"
                    "  const long i = blockIdx.x * blockDim.x + threadIdx.x;\n"
                    "  if (i < n) {\n    c_model::c__flux(atmofab::View<const double, 1>{u + 3 * i, {3}},"
                    " 9.81,\n                     atmofab::View<double, 1>{f + 3 * i, {3}});\n"
                    "  }\n}\n}  // namespace\n"
                    "void launch(const double* u, double* f, long n) { faces<<<1, 32>>>(u, f, n); }\n"
                    "int main() { return 0; }\n")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "c_model.cuh").write_text(
                cpp_header.render("c", _public_api(_POINTWISE), spec_kind="component"))
            (d / "c_model.cu").write_text(_POINTWISE_MODEL)
            (d / "p.cu").write_text(consumer)

            def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.run(argv, cwd=tmp, capture_output=True, text=True, check=False)
            lint = run(list(nvcc_lint.source_argv(["./c_model.cu", "./p.cu"])))
            self.assertEqual(0, lint.returncode, lint.stderr[-2000:])
            (d / ".m").mkdir()
            syntax = run(nvcc_syntax.argv(standard="c++17", scratch_dir=".m", openmp=False,
                                          promotions=(), architecture=None,
                                          sources=["c_model.cu", "p.cu"]))
            self.assertEqual(0, syntax.returncode, syntax.stderr[-2000:])
            flags = cpp_control_file.flags("c++17", None).replace("$(OBJDIR)", ".").split()
            for src in ("c_model.cu", "p.cu"):
                built = run([NVCC, *flags, "-c", src, "-o", src.replace(".cu", ".o")])
                self.assertEqual(0, built.returncode, built.stderr[-2000:])
            linked = run([NVCC, *flags, "c_model.o", "p.o", "-o", "p_bin"])
            self.assertEqual(0, linked.returncode, linked.stderr[-2000:])
            without = run([a for a in nvcc_lint.source_argv(["./p.cu"]) if a != "-rdc=true"])
            self.assertNotEqual(0, without.returncode)
            self.assertIn("Unresolved extern function", without.stderr + without.stdout)

    def test_self_check_accepts_the_declared_flags(self) -> None:
        proc = subprocess.run(list(nvcc_lint.self_check_argv("/unused")), capture_output=True,
                              text=True, check=False)
        self.assertIsNone(nvcc_lint.self_check_reason(proc.returncode, proc.stdout, proc.stderr))

    def test_syntax_adapter_and_canary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / nvcc_syntax.CANARY_FILENAME).write_text(nvcc_syntax.CANARY_SOURCE)
            (Path(tmp) / ".m").mkdir()
            argv = nvcc_syntax.argv(standard="c++17", scratch_dir=".m", openmp=False,
                                    promotions=(), architecture="sm_90",
                                    sources=[nvcc_syntax.CANARY_FILENAME])
            self.assertEqual(0, subprocess.run(argv, cwd=tmp, capture_output=True, check=False).returncode)
            bad = [a.replace("c++17", "17") for a in argv]
            self.assertNotEqual(0, subprocess.run(bad, cwd=tmp, capture_output=True, check=False).returncode)
            self.assertEqual(sorted(p.name for p in pathlib.Path(tmp).iterdir()),
                             sorted([".m", nvcc_syntax.CANARY_FILENAME]))


    def test_no_macro_the_permitted_headers_define_can_emit_a_pragma_unrefused(self) -> None:
        """The reserved-identifier rule and the header allowlist of the preprocessor gate rest on
        one measurement, taken here against the installed driver: every macro that the permitted
        `<...>` headers (and the runtime header the driver includes into every source) define,
        and whose expansion reaches `_Pragma` / `__pragma` through any chain of macros, is refused
        by name — except `sigmask`, which expands to a `GCC warning` pragma that RAISES a
        diagnostic, so using it fails the lint rather than silencing it."""
        import re

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "all.cu"
            src.write_text("".join(f"#include <{h}>\n" for h in sorted(cpp_source.STANDARD_HEADERS)))
            proc = subprocess.run([nvcc_syntax.EXECUTABLE, "-std=c++17", "-E", "-Xcompiler", "-dM",
                                   str(src)], capture_output=True, text=True, check=False, timeout=600)
        self.assertEqual(0, proc.returncode, proc.stderr[-2000:])
        definitions = {}
        for line in proc.stdout.splitlines():
            m = re.match(r"#define (\w+)(?:\([^)]*\))? ?(.*)", line)
            if m:
                definitions[m.group(1)] = m.group(2)
        self.assertGreater(len(definitions), 1000)
        emitting = {name for name, body in definitions.items()
                    if re.search(r"\b(?:_Pragma|__pragma)\b", body)}
        self.assertIn("__NV_SILENCE_DEPRECATION_BEGIN", emitting)
        grew = True
        while grew:
            grew = False
            for name, body in definitions.items():
                if name not in emitting and emitting & set(re.findall(r"\b\w+\b", body)):
                    emitting.add(name)
                    grew = True
        self.assertEqual(set(), emitting & cpp_source.RESERVED_IDENTIFIERS_ALLOWED)
        unrefused = {name for name in emitting
                     if not cpp_source.preprocessor_violations(Path("x.cu"), f"{name}\n")}
        self.assertLessEqual(unrefused, {"sigmask"}, sorted(unrefused))
        if unrefused:
            self.assertIn("__glibc_macro_warning", definitions["sigmask"])
            self.assertIn("GCC warning", definitions["__glibc_macro_warning"])

    def test_every_rendered_header_in_the_tree_is_lint_clean(self) -> None:
        """The host-rendered header is CONTEXT to the lint (a leaf source includes it), so a
        warning in it would be charged to the leaf: every §5.1 in spec/ renders to a header a
        source including it lints clean under the declared rule set."""
        specs = sorted(p for p in (REPO_ROOT / "spec").rglob("controlled_spec.md") if _section51(p))
        for path in specs:
            with self.subTest(spec=path.parent.name):
                struct, _err = cs.load_structured_signatures(_section51(path))
                spec_id = path.parent.name
                with tempfile.TemporaryDirectory() as tmp:
                    (Path(tmp) / cpp_header.basename(spec_id)).write_text(
                        cpp_header.render(
                            spec_id, _public_api(struct),
                            spec_kind=path.relative_to(REPO_ROOT / "spec").parts[0]))
                    (Path(tmp) / "x.cu").write_text(f'#include "{cpp_header.basename(spec_id)}"\n')
                    proc = subprocess.run(list(nvcc_lint.source_argv(["./x.cu"])), cwd=tmp,
                                          capture_output=True, text=True, check=False)
                    self.assertEqual(0, proc.returncode, proc.stderr[-2000:])

    def test_a_harness_shaped_node_passes_every_gate_and_builds(self) -> None:
        """The whole local path of a `cuda_cpp` harness, with the real driver: the host header and
        Makefile, a leaf model and runner written to the binding, the preprocessor allowlist, the
        §5.1 pin, the lint and the syntax stage through the build-runtime server, then `make`."""
        import sys

        from tools.backends import registry
        from tools.codegen_bundle import derive_build_graph
        sys.path.insert(0, str(REPO_ROOT / "mcp_servers"))
        import build_runtime_server as server

        model = ('#include <cstdio>\n#include "h_model.cuh"\nnamespace h_model {\n'
                 "void h__run(atmofab::View<dp, 1> u, h__cb cb, bool& ok) {\n"
                 "  for (long i = 0; i < u.extent[0]; ++i) { u.data[i] += 1.0; }\n"
                 "  cb(0.0);\n  ok = true;\n}\n"
                 "std::string h__emit(dp x) {\n  char buf[32];\n"
                 '  std::snprintf(buf, sizeof buf, "%.16e", x);\n  return buf;\n}\n'
                 "}  // namespace h_model\n")
        runner = ('#include <cstdio>\n#include "h_model.cuh"\n'
                  "static void on_step(h_model::dp t) { (void)t; }\n"
                  "int main(int argc, char** argv) {\n  (void)argc;\n  (void)argv;\n"
                  "  double d[2] = {0.0, 0.0};\n"
                  "  atmofab::View<h_model::dp, 1> v{d, {2}};\n  bool ok = false;\n"
                  "  h_model::h__run(v, on_step, ok);\n"
                  "  std::puts(h_model::h__emit(d[0]).c_str());\n  return ok ? 0 : 1;\n}\n")
        doc = {"optimization_unit": {"members": ["infrastructure/h@0.1.0"]}, "files": [
            {"logical_path": "h_model.cu", "role": "model", "language": "cuda_cpp",
             "member_node_key": "infrastructure/h@0.1.0", "content": model, "modules": ["h_model"]},
            {"logical_path": "h_runner.cu", "role": "runner", "language": "cuda_cpp",
             "member_node_key": "infrastructure/h@0.1.0", "content": runner, "modules": []}]}
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            for entry in doc["files"]:
                (src / entry["logical_path"]).write_text(entry["content"])
            (src / "h_model.cuh").write_text(_HEADER)
            for path in cpp_source.leaf_sources(src):
                self.assertEqual([], cpp_source.preprocessor_violations(path, path.read_text()))
            ops, types, ifaces, _e = cs.parse_interface_stanzas(cs.render_signatures(_HARNESS_LIKE))
            pin: list[str] = []
            cs.generated_source_violations(
                model_files=[src / "h_model.cu"], target=src / "h_model.cu",
                ir_kind="infrastructure", op_stanzas=ops, type_stanzas=types,
                proto_stanzas=ifaces, module_parameters=_HARNESS_LIKE["module_parameters"],
                procedures=_HARNESS_LIKE["procedures"], violations=pin)
            self.assertEqual([], pin)
            lint = server.tool_run_linter({"preset": "nvcc", "project_dir": str(src)})
            self.assertTrue(lint["ok"], lint.get("stderr"))
            syntax = server.tool_run_syntax_check({
                "compiler": "nvcc", "std": "c++17", "architecture": "sm_90",
                "project_dir": str(src)})
            self.assertTrue(syntax["ok"] and not syntax["skipped"], syntax.get("stderr"))
            graph = derive_build_graph(doc, toolchain={"language": "cuda_cpp", "compiler": "nvcc"})
            rules = registry.capability_module("language", "cuda_cpp", "control_file").rules(
                standard="c++17", parallel_backend="cuda", architecture="sm_90")
            makefile = registry.capability_module("build_system", "make", "control_file") \
                .render_from_graph(rules=rules, compiler="nvcc", bin_name="h_runner",
                                   cases_default="c1", graph=graph)
            (src / "Makefile").write_text(makefile)
            build = subprocess.run(["make", "OBJDIR=../obj", "BINDIR=../bin", "all"], cwd=src,
                                   capture_output=True, text=True, check=False, timeout=600)
            self.assertEqual(0, build.returncode, build.stdout[-2000:] + build.stderr[-2000:])
            self.assertTrue((Path(tmp) / "bin" / "h_runner").is_file())

if __name__ == "__main__":
    unittest.main()
