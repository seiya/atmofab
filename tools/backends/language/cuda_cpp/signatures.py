"""The `signatures` capability for CUDA C++ (issue #289, R4-b PR-4).

How the language-neutral structured signature form (`tools/structured_signatures.py`) LOWERS to
CUDA C++ (which the host-rendered header, `header.py`, is made of), how a C++ text is read back
into comparable stanzas, and the `Generate.static` rules that pin a node's header and model source
against §5.1. The binding every rule here implements is stated
for a reader in `docs/backends/language/cuda_cpp/BUNDLE_BINDING.md` §2; this module is its
executable form, and the lowering table below is the one place it is written in code.

THE LOWERING (argument / component / result), with `K` a kind symbol (a module parameter) or the
language default:

    neutral                                  argument            component / result
    real / integer / logical, rank 0,  in    K name              K
    ... rank 0, out / inout                  K& name             —
    real / integer / logical, rank R >= 1    atmofab::View<const K, R> (in) / View<K, R>
                                             (rank 1 component / result: std::vector<K>)
    ... rank 1, alloc                        const std::vector<K>& (in) / std::vector<K>&
    string, rank 0                           const std::string& (in) / std::string&   std::string
    derived T, rank 0                        const T& (in) / T&                       T
    string / derived, rank 1                 const std::vector<X>& (in) / std::vector<X>&  std::vector<X>
    procedure (an `interfaces` entry P)      P name              —
    subroutine / function                    void name(...) / <result type> name(...)
    module parameter  dp = float64           using dp = double;  (float32 -> float)
    module parameter  n = 64                 inline constexpr int n = 64;
    interfaces entry P                       using P = <ret> (*)(<params>);
    type T                                   struct T { <component>; ... };

`atmofab::View<T, R>` is the rank-R, column-major, non-owning array view every rendered header
defines (data pointer plus extents); the neutral `dims` of an argument are NOT part of the C++
type, because a view's extents are run-time values, so they are not pinned here. A neutral string
length is likewise not part of the type (`std::string` carries its own). A default kind is `float`
for `real` and `int` for `integer`, and a named kind must be a float-valued module parameter (the
only kind that is a C++ type). What has no row above — an array component or result of rank > 1,
a string / derived argument of rank > 1, an allocatable numeric argument of rank > 1, a `logical`
with a kind — has no C++ lowering and is refused (`SignatureParseError`), as is a name that is a
C++ keyword.

THE STANZA a text is read into (`parse_interface_stanzas`), per symbol, is a list of canonical
lines — the header first, then one line per parameter / data member:

    procedure   "<return type> <name>(<arg names in order>)", "<type> <arg>", ...
    type        "struct <name>", "<type> <member>", ...          (member order is significant)
    interface   "using <name> = <ret>(*)(<arg names>)", "<type> <arg>", ...

and an ATOM is a line with every whitespace character removed (`stanza_atoms`). Rendering a
signature and reading the rendered text back therefore produce the same atoms as reading a
source that spells the same declaration with other spacing; the ORDER of a procedure's arguments
is pinned by the header atom, so a procedure may be compared as a set. Comparison is
case-SENSITIVE: C++ identifiers are.

Imports the neutral core's structured form; no other package.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tools.backends.language.cuda_cpp import declarations as cpp_decls
from tools.structured_signatures import (  # noqa: F401  (re-export: the capability contract)
    SignatureParseError,
    load_structured_signatures,
)
from tools.structured_signatures import (
    validate_module_parameter as _validate_module_parameter,
)
from tools.structured_signatures import validate_procedure as _validate_procedure
from tools.structured_signatures import validate_struct as _validate_struct
from tools.structured_signatures import validate_symbol as _validate_symbol

#: The language's name as the gates' messages spell it.
LANGUAGE_DISPLAY_NAME = "CUDA C++"

#: The array view type the target's harness defines (see the module docstring).
VIEW_TYPE = "atmofab::View"

#: C++ keywords (C++17) and the CUDA execution-space / memory-space specifiers. A §5.1 name equal
#: to one of them cannot be declared in C++, so the lowering refuses it.
CPP_KEYWORDS = frozenset(["alignas", "alignof", "and", "and_eq", "asm", "auto", "bitand", "bitor", "bool", "break", "case", "catch", "char", "char8_t", "char16_t", "char32_t", "class", "compl", "concept", "const", "consteval", "constexpr", "constinit", "const_cast", "continue", "co_await", "co_return", "co_yield", "decltype", "default", "delete", "do", "double", "dynamic_cast", "else", "enum", "explicit", "export", "extern", "false", "float", "for", "friend", "goto", "if", "inline", "int", "long", "mutable", "namespace", "new", "noexcept", "not", "not_eq", "nullptr", "operator", "or", "or_eq", "private", "protected", "public", "register", "reinterpret_cast", "requires", "return", "short", "signed", "sizeof", "static", "static_assert", "static_cast", "struct", "switch", "template", "this", "thread_local", "throw", "true", "try", "typedef", "typeid", "typename", "union", "unsigned", "using", "virtual", "void", "volatile", "wchar_t", "while", "xor", "xor_eq", "__global__", "__device__", "__host__", "__shared__", "__constant__", "__managed__"])

_NEUTRAL_KIND_TO_CPP = {"float64": "double", "float32": "float"}
_DEFAULT_SCALAR = {"real": "float", "integer": "int", "logical": "bool"}


def _require_cpp_name(name: str, ctx: str) -> str:
    if name in CPP_KEYWORDS:
        raise SignatureParseError(
            f"{ctx} '{name}' is a C++ keyword, so it cannot be declared in {LANGUAGE_DISPLAY_NAME}"
            " — rename it in §5.1 (a name must be declarable in every target language)")
    return name


def _scalar(spec: dict[str, Any], ctx: str) -> str:
    """The C++ spelling of a scalar value of `spec` (no reference, no const)."""
    t = spec["type"]
    if t in _DEFAULT_SCALAR:
        kind = spec.get("kind")
        if kind is None:
            return _DEFAULT_SCALAR[t]
        if t == "logical":
            raise SignatureParseError(
                f"{ctx}: a `logical` with a kind has no {LANGUAGE_DISPLAY_NAME} lowering")
        return _require_cpp_name(str(kind).strip(), f"{ctx}.spec.kind")
    if t == "string":
        return "std::string"
    if t == "derived":
        return _require_cpp_name(spec["name"], f"{ctx}.spec.name")
    raise SignatureParseError(f"{ctx}: a `{t}` value has no {LANGUAGE_DISPLAY_NAME} lowering here")


def _argument_type(ent: dict[str, Any], ctx: str) -> str:
    spec = ent["spec"]
    t = spec["type"]
    rank = ent.get("rank", 0) or 0
    intent = ent.get("intent")
    reading = intent == "in"
    if t == "procedure":
        return _require_cpp_name(spec["interface"], f"{ctx}.spec.interface")
    if rank == 0:
        base = _scalar(spec, ctx)
        if t in _DEFAULT_SCALAR:
            return base if reading else f"{base}&"
        return f"const {base}&" if reading else f"{base}&"
    if t in _DEFAULT_SCALAR:
        base = _scalar(spec, ctx)
        if spec.get("alloc"):
            # The callee may size an allocatable array, which a view cannot do: a rank-1 one is
            # a `std::vector`, and no higher rank has a lowering.
            if rank != 1:
                raise SignatureParseError(
                    f"{ctx}: an allocatable rank-{rank} numeric argument has no "
                    f"{LANGUAGE_DISPLAY_NAME} lowering (only rank 1, to std::vector)")
            return f"const std::vector<{base}>&" if reading else f"std::vector<{base}>&"
        return f"{VIEW_TYPE}<{'const ' if reading else ''}{base}, {rank}>"
    if rank == 1:
        vec = f"std::vector<{_scalar(spec, ctx)}>"
        return f"const {vec}&" if reading else f"{vec}&"
    raise SignatureParseError(
        f"{ctx}: a rank-{rank} `{t}` argument has no {LANGUAGE_DISPLAY_NAME} lowering (only rank 1 "
        "lowers, to std::vector)")


def _value_type(ent: dict[str, Any], ctx: str) -> str:
    """A component's or a function result's type."""
    spec = ent["spec"]
    rank = ent.get("rank", 0) or 0
    if rank == 0:
        return _scalar(spec, ctx)
    if rank == 1:
        return f"std::vector<{_scalar(spec, ctx)}>"
    raise SignatureParseError(
        f"{ctx}: a rank-{rank} `{spec['type']}` component or result has no "
        f"{LANGUAGE_DISPLAY_NAME} lowering (only rank 1, to std::vector)")


def _render_params(args: list[dict[str, Any]], ctx: str) -> list[str]:
    return [f"{_argument_type(a, f'{ctx}.args[{i}]')} {_require_cpp_name(a['name'], f'{ctx}.args[{i}].name')}"
            for i, a in enumerate(args)]


def _render_procedure(proc: dict[str, Any], ctx: str = "procedure") -> list[str]:
    name = _require_cpp_name(proc["name"], f"{ctx}.name")
    ret = "void" if proc["kind"] == "subroutine" else _value_type(proc["result"], f"{ctx}.result")
    params = _render_params(proc.get("args") or [], ctx)
    if not params:
        return [f"{ret} {name}();"]
    return [f"{ret} {name}("] + [f"    {p}," for p in params[:-1]] + [f"    {params[-1]});"]


def _render_type(tdef: dict[str, Any], ctx: str = "type") -> list[str]:
    name = _require_cpp_name(tdef["name"], f"{ctx}.name")
    body = [f"    {_value_type(c, f'{ctx}.components[{i}]')} "
            f"{_require_cpp_name(c['name'], f'{ctx}.components[{i}].name')};"
            for i, c in enumerate(tdef.get("components") or [])]
    return [f"struct {name} {{"] + body + ["};"]


def _render_interface_lines(iface: dict[str, Any], ctx: str = "interface") -> list[str]:
    name = _require_cpp_name(iface["name"], f"{ctx}.name")
    ret = "void" if iface["kind"] == "subroutine" else _value_type(iface["result"], f"{ctx}.result")
    params = _render_params(iface.get("args") or [], ctx)
    if not params:
        return [f"using {name} = {ret} (*)();"]
    return ([f"using {name} = {ret} (*)("] + [f"    {p}," for p in params[:-1]]
            + [f"    {params[-1]});"])


def render_module_parameter(mp: dict[str, Any]) -> str:
    """One §5.1 module parameter as its C++ declaration."""
    _validate_module_parameter(mp, "module_parameter")
    name = _require_cpp_name(mp["name"], "module_parameter.name")
    value = str(mp["value"]).strip()
    if value.lower() in _NEUTRAL_KIND_TO_CPP:
        return f"using {name} = {_NEUTRAL_KIND_TO_CPP[value.lower()]};"
    # `inline`: the declaration lives in a header every file of a program includes, and an
    # unreferenced namespace-scope `inline constexpr` draws no unused-variable diagnostic there
    # (measured with the lint rule set; a plain `constexpr` in the including file is #177-D).
    return f"inline constexpr int {name} = {value};"


def _kind_symbols(spec: Any) -> set[str]:
    return {str(spec["kind"]).strip()} if (isinstance(spec, dict) and spec.get("type") in
                                           ("real", "integer", "logical")
                                           and spec.get("kind") is not None) else set()


def _require_type_kinds(struct: dict[str, Any]) -> None:
    """A kind a spec names must not be a module parameter with an INTEGER value: a float-valued
    one lowers to a C++ type (`using dp = double;`), an integer-valued one to a constant, which is
    not a type. A kind that names no module parameter of the block is rendered as the name, as the
    other languages render it (whether it resolves is the compiler's question)."""
    integer_params = {str(mp["name"]).strip() for mp in struct.get("module_parameters") or []
                      if str(mp.get("value")).strip().lower() not in _NEUTRAL_KIND_TO_CPP}
    entities: list[tuple[str, Any]] = []
    for i, tdef in enumerate(struct.get("types") or []):
        entities += [(f"types[{i}].components[{j}]", c.get("spec"))
                     for j, c in enumerate(tdef.get("components") or [])]
    for key in ("interfaces", "procedures"):
        for i, proc in enumerate(struct.get(key) or []):
            entities += [(f"{key}[{i}].args[{j}]", a.get("spec"))
                         for j, a in enumerate(proc.get("args") or [])]
            if isinstance(proc.get("result"), dict):
                entities.append((f"{key}[{i}].result", proc["result"].get("spec")))
    for ctx, spec in entities:
        for kind in _kind_symbols(spec):
            if kind in integer_params:
                raise SignatureParseError(
                    f"{ctx}.spec.kind '{kind}' is a module parameter with an integer value, which "
                    f"has no {LANGUAGE_DISPLAY_NAME} type (a kind lowers to `using <kind> = "
                    "double;` / `float;`, which only a float64 / float32 value gives)")


def render_signatures(struct: dict[str, Any]) -> str:
    """A whole structured §5.1 block as C++ declarations (module parameters, types, prototypes,
    procedures), validated first."""
    _validate_struct(struct)
    _require_type_kinds(struct)
    out: list[str] = [render_module_parameter(mp) for mp in struct.get("module_parameters") or []]
    for i, tdef in enumerate(struct.get("types") or []):
        out += _render_type(tdef, f"types[{i}]")
    for i, iface in enumerate(struct.get("interfaces") or []):
        out += _render_interface_lines(iface, f"interfaces[{i}]")
    for i, proc in enumerate(struct.get("procedures") or []):
        out += _render_procedure(proc, f"procedures[{i}]")
    return "\n".join(out) + "\n"


def render_symbol(sig: dict[str, Any]) -> str:
    """ONE published symbol (a procedure or a type) as C++."""
    _validate_symbol(sig)
    if sig.get("kind") in ("subroutine", "function"):
        return "\n".join(_render_procedure(sig)) + "\n"
    return "\n".join(_render_type(sig)) + "\n"


def render_interface(iface: dict[str, Any]) -> str:
    """ONE `interfaces` entry as its C++ function-pointer alias."""
    _validate_procedure(iface, "interface", allow_procedure_args=False)
    return "\n".join(_render_interface_lines(iface)) + "\n"


validate_module_parameter = _validate_module_parameter


# --- reading a text back into stanzas -----------------------------------------------------------

_FNPTR_RE = re.compile(r"^(?P<ret>.+?)\(\*\)\((?P<params>.*)\)$", re.DOTALL)


def _function_stanza(fn: cpp_decls.Function) -> list[str]:
    names = ", ".join(n for _t, n in fn.params)
    return [f"{fn.returns} {fn.name}({names})"] + [f"{t} {n}" for t, n in fn.params]


def _struct_stanza(st: cpp_decls.Struct) -> list[str]:
    return [f"struct {st.name}"] + [f"{t} {n}" for t, n in st.members]


def _alias_stanza(alias: cpp_decls.Alias) -> list[str] | None:
    m = _FNPTR_RE.match(alias.target)
    if m is None:
        return None
    params = cpp_decls.parse_params(m.group("params"))
    names = ", ".join(n for _t, n in params)
    return ([f"using {alias.name} = {m.group('ret')}(*)({names})"]
            + [f"{t} {n}" for t, n in params])


def _stanzas(decls: cpp_decls.Declarations, namespace: tuple[str, ...] | None) -> tuple[
        dict[str, list[str]], dict[str, list[str]], dict[str, list[str]], list[str]]:
    """The stanzas of the declarations in `namespace` (every namespace when None)."""
    errors = list(decls.errors)

    def here(ns: tuple[str, ...]) -> bool:
        return namespace is None or ns == namespace

    ops: dict[str, list[str]] = {}
    # One function is its declarations and its definition. They agree when their return type and
    # parameter TYPES do — a declaration may leave its parameters unnamed or name them otherwise,
    # which C++ allows — so the overload test compares types only, and the stanza compared
    # against §5.1 (which pins the names) is the DEFINITION's when there is one.
    by_name: dict[str, set[tuple[str, ...]]] = {}
    for fn in decls.functions:
        if not here(fn.namespace):
            continue
        by_name.setdefault(fn.name, set()).add(
            tuple(stanza_atoms([fn.returns, *(ptype for ptype, _n in fn.params)])))
        if fn.name not in ops or fn.defined:
            ops[fn.name] = _function_stanza(fn)
    for name, variants in sorted(by_name.items()):
        if len(variants) > 1:
            errors.append(
                f"procedure '{name}' is declared with {len(variants)} different signatures (an "
                "overload, or a declaration that disagrees with its definition)")
    types: dict[str, list[str]] = {}
    for st in decls.structs:
        if not here(st.namespace):
            continue
        if st.name in types:
            errors.append(f"type '{st.name}' is defined more than once")
            continue
        types[st.name] = _struct_stanza(st)
    ifaces: dict[str, list[str]] = {}
    for alias in decls.aliases:
        if not here(alias.namespace):
            continue
        stanza = _alias_stanza(alias)
        if stanza is None:
            continue
        if alias.name in ifaces:
            errors.append(f"prototype '{alias.name}' is declared more than once")
            continue
        ifaces[alias.name] = stanza
    return ops, types, ifaces, errors


def parse_interface_stanzas(text: str) -> tuple[
        dict[str, list[str]], dict[str, list[str]], dict[str, list[str]], list[str]]:
    """`text` read into `(procedure stanzas, type stanzas, prototype stanzas, errors)`, each keyed
    by name, across every namespace. A procedure declared and defined with one signature is one
    stanza; two different signatures under one name is an error, as is a type or a prototype
    declared twice."""
    return _stanzas(cpp_decls.read(text), None)


def stanza_atoms(lines: list[str]) -> tuple[str, ...]:
    """Each canonical line of a stanza as its comparison atom: every whitespace character
    removed, a trailing `,` / `;` / `{` dropped. Empty lines contribute nothing."""
    atoms: list[str] = []
    for line in lines:
        atom = re.sub(r"\s+", "", line).rstrip(",;{")
        if atom:
            atoms.append(atom)
    return tuple(atoms)


def stanza_line_list(lines: list[str]) -> tuple[str, ...]:
    """A stanza's atoms in order (a type's member order is part of its layout)."""
    return stanza_atoms(lines)


def stanza_line_set(lines: list[str]) -> frozenset[str]:
    """A stanza's atoms as a set (a procedure's argument order is in its header atom)."""
    return frozenset(stanza_atoms(lines))


# --- the Generate.static source pin ------------------------------------------------------------

def generated_source_violations(
    *,
    model_files: list[Path],
    target: Path,
    ir_kind: str,
    op_stanzas: dict[str, list[str]],
    type_stanzas: dict[str, list[str]],
    proto_stanzas: dict[str, list[str]],
    module_parameters: list[dict[str, Any]],
    violations: list[str],
) -> None:
    """The source half of the validator's `Generate.static` signature gate, in CUDA C++: pin the
    node's published surface against §5.1 already rendered to C++ stanzas.

    The surface is DECLARED by the host-rendered header `<model stem>.cuh` beside the model source
    (`header.render`, from the IR `public_api`) and DEFINED by the model source, both in namespace
    `<model stem>` (BUNDLE_BINDING.md §1). The two are read together, so the rules hold of what a
    consumer compiles against: exactly one model file when §5.1 publishes anything; the header
    present; every §5.1 type a struct of that namespace with exactly the pinned members in order,
    defined once; every §5.1 procedure declared with exactly the pinned header and parameters and
    DEFINED in the model source with the same types (a definition whose types differ from the
    declaration is another overload, refused); every prototype a function-pointer alias with
    exactly the pinned atoms, and no function of its name defined; every module parameter declared
    exactly once, as rendered."""
    if (op_stanzas or type_stanzas) and len(model_files) != 1:
        violations.append(
            f"{target}: this {ir_kind} node's published surface cannot be pinned to one "
            f"publisher — {len(model_files)} model source files were resolved and exactly one "
            "is expected")
        return
    if not model_files:
        return
    model_file = model_files[0]
    namespace = (model_file.stem,)
    header_path = model_file.parent / f"{model_file.stem}.cuh"
    try:
        header_text = header_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        violations.append(
            f"{target}: the host-rendered header {header_path.name} is not beside the model "
            "source, so the published surface has no declaration to pin (a host fault: the "
            "header is written with the bundle's files)")
        return
    model_text = model_file.read_text(encoding="utf-8", errors="ignore")
    decls = cpp_decls.read(header_text + "\n" + model_text)
    src_ops, src_types, src_ifaces, src_errors = _stanzas(decls, namespace)
    for err in src_errors:
        violations.append(
            f"{target}: the model source cannot be compared with controlled_spec §5.1 ({err}) — "
            f"the host-rendered {header_path.name} declares the published surface; define each "
            "published procedure once, with exactly its declared parameter types, and declare no "
            "published type, prototype or module parameter again")
    if src_errors:
        return
    model_defined = {fn.name for fn in cpp_decls.read(model_text).functions
                     if fn.namespace == namespace and fn.defined}

    for name in sorted(type_stanzas):
        have = src_types.get(name)
        if have is None or stanza_line_list(have) != stanza_line_list(type_stanzas[name]):
            violations.append(
                f"{target}: type '{name}' of namespace `{namespace[0]}` does not match "
                "controlled_spec §5.1 (the host-rendered header declares it; if it drifts, the "
                "header was not rendered from this node's IR)")
    for name in sorted(op_stanzas):
        have_stanza = src_ops.get(name)
        if have_stanza is None:
            violations.append(
                f"{target}: controlled_spec §5.1 procedure '{name}' is not declared in namespace "
                f"`{namespace[0]}` (the host-rendered header declares it)")
            continue
        want = stanza_line_set(op_stanzas[name])
        have = stanza_line_set(have_stanza)
        if have != want:
            violations.append(
                f"{target}: procedure '{name}' drifts from controlled_spec §5.1 — missing "
                f"{sorted(want - have)}, extra {sorted(have - want)} (compared with whitespace "
                "removed; the first atom is the header, which pins the return type and the "
                "argument NAMES in order, and a specifier on it such as `__device__` is a "
                "difference) — define it with exactly the declaration in "
                f"{header_path.name}")
        if name not in model_defined:
            violations.append(
                f"{target}: controlled_spec §5.1 procedure '{name}' is declared but never DEFINED "
                f"in namespace `{namespace[0]}` of the model source — give it a body there, with "
                f"exactly the parameter types {header_path.name} declares")
    for name in sorted(proto_stanzas):
        have_proto = src_ifaces.get(name)
        if have_proto is None or stanza_line_set(have_proto) != stanza_line_set(
                proto_stanzas[name]):
            violations.append(
                f"{target}: prototype '{name}' of namespace `{namespace[0]}` does not match "
                "controlled_spec §5.1's `interfaces` entry (the host-rendered header declares it)")
        if name in model_defined:
            violations.append(
                f"{target}: the model source DEFINES '{name}', which controlled_spec §5.1 "
                "declares as a prototype (an `interfaces` entry) — a prototype is the shape of the "
                "function a CALLER passes, and the model must not implement it")

    for mp in module_parameters:
        name = str(mp.get("name") or "").strip() if isinstance(mp, dict) else ""
        if not name:
            continue
        try:
            pinned = render_module_parameter(mp)
        except SignatureParseError as exc:
            violations.append(
                f"{target}: controlled_spec §5.1 declares a module parameter the language backend "
                f"cannot lower ({exc}) — re-certify the harness so §5.1 carries a neutral "
                "parameter value the generated source can be pinned against")
            continue
        spelled = ([f"using {a.name} = {a.target}" for a in decls.aliases
                    if a.namespace == namespace and a.name == name]
                   + [v.statement for v in decls.variables
                      if v.namespace == namespace and v.name == name])
        pinned_atom = stanza_atoms([pinned])[0]
        if not any(stanza_atoms([s])[0] == pinned_atom for s in spelled):
            violations.append(
                f"{target}: the §5.1 module parameter declaration `{pinned}` is not in namespace "
                f"`{namespace[0]}` (the host-rendered header declares it)")
        elif len(spelled) > 1:
            listed = ", ".join(f"`{s}`" for s in sorted(spelled))
            violations.append(
                f"{target}: the §5.1 module parameter `{name}` is bound {len(spelled)} times in "
                f"namespace `{namespace[0]}` — {listed} — and `{pinned}` (in the host-rendered "
                "header) is what §5.1 pins; delete the model source's own binding")


# --- a certified source's published interface, as a consumer's leaf is shown it ----------------

def _rank_of(ctype: str) -> int:
    m = re.search(r"View<.*,\s*(\d+)>", ctype)
    if m:
        return int(m.group(1))
    return 1 if "std::vector<" in ctype else 0


def published_interface(source_text: str, name: str) -> dict[str, Any] | None:
    """The call-site interface of the function `name` a certified source defines: its header as
    one line, its argument order, and each argument's type and rank (`rank` read off the lowered
    type: a view's rank, 1 for a `std::vector`, else 0). None when it is not defined. Never
    raises."""
    try:
        decls = cpp_decls.read(source_text)
    except Exception:  # noqa: BLE001 - orientation only (the caller's contract)
        return None
    candidates = [fn for fn in decls.functions if fn.name == name and fn.defined]
    if len(candidates) != 1:
        return None
    fn = candidates[0]
    params = ", ".join(f"{t} {n}".strip() for t, n in fn.params)
    return {
        "interface": f"{fn.returns} {fn.name}({params})",
        "argument_order": [n for _t, n in fn.params],
        "arguments": [{"name": n, "type": t, "rank": _rank_of(t)} for t, n in fn.params],
    }


def prefixed_procedures(source_text: str, prefix: str) -> list[str]:
    """The distinct, source-ordered names of the `prefix`-named functions a certified source
    defines."""
    try:
        decls = cpp_decls.read(source_text)
    except Exception:  # noqa: BLE001 - orientation only
        return []
    out: list[str] = []
    for fn in decls.functions:
        if fn.defined and fn.name.startswith(prefix) and fn.name not in out:
            out.append(fn.name)
    return out


def procedure_interface(arg: dict[str, Any]) -> str | None:
    """The prototype a function-pointer argument references, as a resolved fact states it on the
    argument (`procedure_interface`), else None. `published_interface` states none today — a C++
    argument's type IS the alias name, and the prototype listing is a physics consumer's, which
    this backend does not serve yet."""
    name = arg.get("procedure_interface")
    return name.strip() if isinstance(name, str) and name.strip() else None


def dependency_operations_header(*, detailed: bool, procedure_argument: bool) -> str:
    """The paragraph that heads a consumer's published-dependency-operation lines."""
    del procedure_argument  # an alias-typed argument needs no paragraph of its own here
    text = (
        "**Published dependency operations (conductor-resolved from each dependency's "
        "CERTIFIED source — the exact source Build will compile):** C++ arguments are "
        "positional; call each operation, qualified by its model's namespace, with EXACTLY this "
        "argument order. A wrong order builds against a type mismatch and fails the build "
        "(routed back to Generate). For generate.generate this is authoring-binding; for "
        "verify/validate it is the authoritative order to check the emitted call against.")
    if detailed:
        text += (" Each argument's declared type and rank is listed under its header — pass an "
                 f"actual of that type: a `{VIEW_TYPE}` of exactly that rank, never a view of "
                 "another rank.")
    return text


def prototype_heading(name: str) -> str:
    """The line above a prototype listing."""
    return (f"    prototype `{name}` — the function you pass for the argument above must have "
            "EXACTLY this parameter list:")


def argument_detail_lines(arguments: Any, *,
                          carried_prototypes: frozenset[str] = frozenset()) -> list[str]:
    """Indented `type / rank` lines for one published operation's arguments, or `[]` when
    `arguments` is absent or carries no resolved rank."""
    del carried_prototypes  # no argument names a separate prototype listing in C++
    if not isinstance(arguments, list) or not arguments:
        return []
    lines: list[str] = []
    for arg in arguments:
        if not isinstance(arg, dict) or not str(arg.get("name", "")).strip():
            continue
        rank = arg.get("rank")
        if not isinstance(rank, int) or isinstance(rank, bool):
            continue
        lines.append(f"    - `{str(arg['name']).strip()}`: `{str(arg.get('type', '')).strip()}`"
                     f" (rank {rank})")
    return lines
