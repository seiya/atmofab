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


_HEADER = cpp_header.render("h", _public_api(_HARNESS_LIKE))

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
                "kind": "subroutine", "name": "new", "args": []}}]})
        with self.assertRaises(cs.SignatureParseError):
            cpp_header.render("h", {"signatures": [{"symbol": "x"}]})


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
                module_parameters=_HARNESS_LIKE["module_parameters"], violations=out)
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
                              "different signatures"),
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
                type_stanzas=types, proto_stanzas=ifaces, module_parameters=[], violations=out)
        self.assertTrue(any("exactly one is expected" in v for v in out), out)


class DependencyInterfaceTests(unittest.TestCase):
    def test_published_interface_and_prefixed_procedures(self) -> None:
        facts = cs.published_interface(_GOOD_MODEL, "h__run")
        self.assertEqual(["u", "cb", "ok"], facts["argument_order"])
        self.assertEqual([1, 0, 0], [a["rank"] for a in facts["arguments"]])
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
        self.assertEqual([], cpp_source.checks_harness_isolation_violations(
            path, _CHECKS_SOURCE, []))

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
        qualified = _CHECKS_SOURCE.replace("void get_time(double& t) { t = 0.0; }", "") + (
            "void p_checks::get_time(double& when) { when = 0.0; }\n")
        self.assertIn("get_time", cpp_source.checks_module_abi_facts(qualified, "p")[0])

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

    def test_isolation_refuses_the_harness_and_file_io(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "p_model.cu"
            model.write_text('#include "harness_cpp_gpu_model.cuh"\nint x;\n')
            out = cpp_source.checks_harness_isolation_violations(
                Path("c.cu"), _CHECKS_SOURCE, [model])
            self.assertTrue(any(str(model) in v and "harness" in v for v in out), out)
            model.write_text("void f() { harness_cpp_gpu_model::harness_cpp_gpu__emit_real(1.0); }\n")
            out = cpp_source.checks_harness_isolation_violations(
                Path("c.cu"), _CHECKS_SOURCE, [model])
            self.assertTrue(any(str(model) in v for v in out), out)
            model.write_text('// #include "harness_cpp_gpu_model.cuh"\n'
                             'const char* m = "harness_cpp_gpu_model::";\n')
            self.assertEqual([], cpp_source.checks_harness_isolation_violations(
                Path("c.cu"), _CHECKS_SOURCE, [model]))
        for io in ("std::ofstream out(\"x\");", "FILE* f = fopen(\"x\", \"w\");",
                   "std::fstream f(\"x\", std::ios::out);",
                   "std::basic_ofstream<char> f(\"x\");", "std::wofstream f(\"x\");",
                   "std::system(\"cp a b\");", "popen(\"ls\", \"r\");",
                   "std::filesystem::copy_file(\"a\", \"b\");", "std::rename(\"a\", \"b\");",
                   "std::remove(\"a\");"):
            text = _CHECKS_SOURCE.replace("double s = 0.0;", f"double s = 0.0;\nvoid w() {{ {io} }}")
            with self.subTest(io):
                out = cpp_source.checks_harness_isolation_violations(Path("c.cu"), text, [])
                self.assertTrue(any("must not do file I/O" in v for v in out), out)
        # A name or a literal is not an opener (over-refusal probes).
        for clean in ("double opened = 0.0;", "const char* m = \"std::system(x)\";",
                      "double removed_mass = 1.0;"):
            text = _CHECKS_SOURCE.replace("double s = 0.0;", f"double s = 0.0;\n{clean}")
            with self.subTest(clean):
                self.assertEqual([], cpp_source.checks_harness_isolation_violations(
                    Path("c.cu"), text, []))

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
            "one output unassigned": ("void p__f(double& a, double& b, double x) "
                                      "{ a = 1.0; b = b * x; }", False),
            "input dependent": ("void p__f(double& a, double& b, double x) "
                                "{ a = 1.0; b = x; }", False),
            "boolean literal": ("void p__f(bool& ok, double& a, double x) "
                                "{ ok = true; a = 2.0; (void)x; }", True),
            "suffixed literal": ("void p__f(float& a, double x) { a = 0.5f; (void)x; }", True),
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
             "-c", "a.cu"],
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
                        cpp_header.render(spec_id, _public_api(struct)))
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
                violations=pin)
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
