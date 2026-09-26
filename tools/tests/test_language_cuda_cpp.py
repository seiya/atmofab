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
from tools.backends.language.cuda_cpp import declarations as cpp_decls
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
        self.assertEqual("constexpr int n = 64;", cs.render_module_parameter({"name": "n", "value": 64}))
        proto = {"kind": "subroutine", "name": "rhs", "args": [_arg("t", "real", kind="dp")]}
        self.assertEqual("using rhs = void (*)(\n    dp t);\n", cs.render_interface(proto))
        with_proc = {"kind": "subroutine", "name": "d__a",
                     "args": [{"name": "f", "spec": {"type": "procedure", "interface": "rhs"}}]}
        self.assertEqual("rhs f", self._param(with_proc["args"][0]))

    def test_a_shape_with_no_lowering_is_refused(self) -> None:
        refused = [
            {"kind": "subroutine", "name": "d__a",
             "args": [_arg("v", "string", rank=2, len="assumed")]},
            {"kind": "subroutine", "name": "d__a",
             "args": [_arg("v", "real", rank=1, kind="dp", intent="inout", alloc=True)]},
            {"kind": "subroutine", "name": "d__a", "args": [_arg("b", "logical", kind="k")]},
            {"kind": "subroutine", "name": "new", "args": []},
            {"kind": "subroutine", "name": "d__a", "args": [_arg("class", "integer")]},
            {"name": "T", "components": [{"name": "a", "rank": 1,
                                          "spec": {"type": "real", "kind": "dp"}}]},
            {"kind": "function", "name": "d__r", "args": [],
             "result": {"name": "r", "rank": 1, "spec": {"type": "real", "kind": "dp"}}},
        ]
        for sig in refused:
            with self.subTest(sig=sig.get("name")), self.assertRaises(cs.SignatureParseError):
                cs.render_symbol(sig)


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

_GOOD_MODEL = """#pragma once
#include <string>
namespace atmofab { template <class T, int R> struct View { T* data; long extent[R]; }; }
namespace h_model {
using dp = double;
constexpr int n = 64;
struct h__rec {
  std::string id;
  dp v;
};
using h__cb = void (*)(dp t);
void h__run(atmofab::View<dp, 1> u, h__cb cb, bool& ok) { (void)u; cb(0.0); ok = true; }
std::string h__emit(dp x) { (void)x; return "0"; }
}  // namespace h_model
"""


class GeneratedSourcePinTests(unittest.TestCase):
    def _violations(self, model: str, name: str = "h_model.cu") -> list[str]:
        ops, types, ifaces, errors = cs.parse_interface_stanzas(cs.render_signatures(_HARNESS_LIKE))
        self.assertEqual([], errors)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_text(model, encoding="utf-8")
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
        self.assertEqual([], self._violations(respaced))

    def test_each_drift_is_named(self) -> None:
        cases = {
            "argument order": (_GOOD_MODEL.replace(
                "void h__run(atmofab::View<dp, 1> u, h__cb cb, bool& ok)",
                "void h__run(h__cb cb, atmofab::View<dp, 1> u, bool& ok)"), "drifts from"),
            "argument type": (_GOOD_MODEL.replace("std::string h__emit(dp x)",
                                                  "std::string h__emit(double x)"), "drifts from"),
            "device specifier": (_GOOD_MODEL.replace("std::string h__emit",
                                                     "__device__ std::string h__emit"),
                                 "drifts from"),
            "declared only": (_GOOD_MODEL.replace(
                "std::string h__emit(dp x) { (void)x; return \"0\"; }",
                "std::string h__emit(dp x);"), "never DEFINES"),
            "missing": (_GOOD_MODEL.replace(
                "std::string h__emit(dp x) { (void)x; return \"0\"; }", ""),
                "does not declare controlled_spec §5.1 procedure 'h__emit'"),
            "outside the namespace": (_GOOD_MODEL.replace(
                "std::string h__emit(dp x) { (void)x; return \"0\"; }\n}  // namespace h_model",
                "}  // namespace h_model\nstd::string h__emit(dp x) { (void)x; return \"0\"; }"),
                "declared outside namespace"),
            "member order": (_GOOD_MODEL.replace("  std::string id;\n  dp v;",
                                                 "  dp v;\n  std::string id;"), "type 'h__rec' drifts"),
            "extra member": (_GOOD_MODEL.replace("  dp v;\n", "  dp v;\n  int extra;\n"),
                             "type 'h__rec' drifts"),
            "prototype drift": (_GOOD_MODEL.replace("using h__cb = void (*)(dp t);",
                                                    "using h__cb = void (*)(double t);"),
                                "prototype 'h__cb' drifts"),
            "prototype missing": (_GOOD_MODEL.replace("using h__cb = void (*)(dp t);",
                                                      "typedef int h__cb;"),
                                  "does not declare the controlled_spec §5.1 prototype"),
            "prototype defined": (_GOOD_MODEL.replace(
                "}  // namespace h_model", "void h__cb(dp t) { (void)t; }\n}  // namespace h_model"),
                "DEFINES 'h__cb'"),
            "parameter value": (_GOOD_MODEL.replace("constexpr int n = 64;", "constexpr int n = 32;"),
                                "missing the §5.1 module parameter declaration `constexpr int n = 64;`"),
            "parameter twice": (_GOOD_MODEL.replace("constexpr int n = 64;",
                                                    "constexpr int n = 64;\nconst int n = 64;"),
                                "binds the §5.1 module parameter `n` 2 times"),
            "overload": (_GOOD_MODEL.replace("}  // namespace h_model",
                                             "std::string h__emit(int x) { (void)x; return \"\"; }\n"
                                             "}  // namespace h_model"),
                         "different signatures"),
        }
        for label, (model, needle) in cases.items():
            with self.subTest(label=label):
                self.assertNotEqual(model, _GOOD_MODEL, "the fixture edit did not apply")
                found = self._violations(model)
                self.assertTrue(any(needle in v for v in found), (needle, found))

    def test_a_declaration_may_leave_parameters_unnamed(self) -> None:
        """C++ lets a declaration name its parameters differently from its definition, or not
        at all; the two are one function, compared by the DEFINITION's names."""
        forward = _GOOD_MODEL.replace(
            "struct h__rec {", "void h__run(atmofab::View<dp, 1>, h__cb, bool&);\nstruct h__rec {")
        self.assertNotEqual(forward, _GOOD_MODEL)
        self.assertEqual([], self._violations(forward))

    def test_the_parameter_pin_ignores_spacing(self) -> None:
        self.assertEqual([], self._violations(_GOOD_MODEL.replace(
            "constexpr int n = 64;", "constexpr int n=64;")))

    def test_a_type_defined_twice_is_an_error(self) -> None:
        _o, _t, _i, errors = cs.parse_interface_stanzas(
            "struct T { int a; };\nnamespace { }\nstruct T { int a; };\n")
        self.assertTrue(any("type 'T' is defined more than once" in e for e in errors), errors)

    def test_no_namespace_of_the_file_stem(self) -> None:
        found = self._violations(_GOOD_MODEL, name="other_model.cu")
        self.assertTrue(any("opens no namespace `other_model`" in v for v in found), found)

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
    def test_each_suppression_form_is_refused_and_a_comment_is_not(self) -> None:
        forms = ["#pragma nv_diag_suppress 177", "  # pragma diag_suppress 550",
                 "#pragma GCC diagnostic ignored \"-Wunused\"", "#pragma clang diagnostic push",
                 "#pragma warning(disable: 4100)", "_Pragma(\"GCC diagnostic ignored \\\"-Wall\\\"\")",
                 "__pragma(warning(disable:1))", "#pragma nv_diagnostic push"]
        for form in forms:
            with self.subTest(form=form):
                self.assertEqual(1, len(cpp_source.suppression_violations(Path("x.cu"), form + "\n")))
        self.assertEqual([], cpp_source.suppression_violations(
            Path("x.cu"), "// #pragma GCC diagnostic ignored\n#pragma once\n"
                          "const char* s = \"#pragma nv_diag_suppress\";\n"
                          "/*\n#pragma GCC diagnostic ignored \"-Wall\"\n*/\n"
                          "// _Pragma(\"GCC diagnostic ignored\")\n"
                          "const char* u = \"_Pragma(\";\n"))

    def test_counted_loops_read_code_only(self) -> None:
        text = ("for (int i = 0; i < n; ++i) {}\nfor (auto& x : v) {}\n"
                "// for (int j = 0; j < n; ++j)\nfor (int k = 0; f(k, 1); ++k) {}\n")
        self.assertEqual(2, cpp_source.counted_loops(text))

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
            (Path(tmp) / "h_runner.cu").write_text("#pragma GCC diagnostic ignored \"-Wall\"\n")
            out: list[str] = []
            cpp_source.model_source_gates(node_key="infrastructure/h@0.1.0", model_file=model,
                                          text=model.read_text(), dep_spec_ids=[], violations=out,
                                          multidim_spec_id=None)
            joined = "\n".join(out)
            self.assertIn("many literal metric assignments", joined)
            self.assertIn("hardcoded case_id -> metrics", joined)
            self.assertIn("h_runner.cu:1: in-source diagnostic suppression", joined)
            self.assertNotIn("not implemented", joined)
            out = []
            cpp_source.model_source_gates(node_key="component/c@0.1.0", model_file=model,
                                          text="int x;\n", dep_spec_ids=[], violations=out,
                                          multidim_spec_id=None)
            self.assertTrue(any("not implemented yet" in v for v in out), out)

    def test_physics_gates_refuse_and_the_harness_has_nothing_to_check(self) -> None:
        self.assertTrue(cpp_source.checks_module_declaration_violations(Path("c"), "", "s"))
        self.assertTrue(cpp_source.checks_harness_isolation_violations(Path("c"), "", []))
        self.assertEqual((set(), set(), set()), cpp_source.checks_module_abi_facts("x", "s"))
        self.assertEqual(["a"], cpp_source.unpublished_bound_state("", "s", ["a"]))
        out: list[str] = []
        cpp_source.validate_dependency_operations([Path("m.cu")], [], out)
        self.assertEqual([], out)
        cpp_source.validate_dependency_operations([Path("m.cu")], ["dep"], out)
        self.assertTrue(out)
        self.assertEqual({"a": set()}, cpp_source.source_module_deps([Path("a.cu")]))
        self.assertEqual(["h__run", "h__emit"],
                         cpp_source.published_subroutines(_GOOD_MODEL, "h"))


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
        self.assertIsNotNone(nvcc_lint.unusable_invocation_reason(255, "", ""))
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
        for capability in ("runner_render", "control_file"):
            self.assertFalse(registry.provides("language", "cuda_cpp", capability))
        self.assertEqual("nvcc", registry.linter_for_language("cuda_cpp"))
        prompts = registry.capability_module("language", "cuda_cpp", "prompt_fragments")
        self.assertEqual({"interface_prototypes", "runner_import"},
                         set(prompts.fragments("generate_generate_harness")))
        with self.assertRaises(ValueError):
            prompts.fragments("generate_generate")
        self.assertTrue(prompts.runner_output_document().startswith("# Runner output"))
        abi = registry.capability_module("language", "cuda_cpp", "checks_abi").document()
        self.assertIn("## 5. CUDA C++ legality and gate guards", abi)


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


if __name__ == "__main__":
    unittest.main()
