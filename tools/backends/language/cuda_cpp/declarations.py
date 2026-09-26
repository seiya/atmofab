"""The namespace-scope declarations of a CUDA C++ source (issue #289, R4-b PR-4).

`read(text)` answers what `signatures` needs from a source — the functions (declared and defined),
the structs with their data members, the `using` / `typedef` aliases and the namespace-scope
variables — each with the namespace it sits in. It reads `lines.mask` + `lines.strip_preprocessor`
of the text, so nothing inside a comment, a literal or a directive is seen, and it tracks nothing
but brackets: a namespace-scope item ends at a `;` at bracket depth 0 or at the `}` closing a body.

WHAT IT DOES NOT READ, stated because a reader that silently skips is a gate that silently passes:
a `template` item (its whole extent is skipped — the published surface of a node is never a
template, and the harness's own view class template is not a §5.1 symbol), an `enum`, an item
inside a function body, and a function-pointer VARIABLE (`void (*fp)(int);`). Each of those is
reported nowhere; a published symbol spelled as one of them is therefore "not found", which is a
refusal, never a pass.

Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tools.backends.language.cuda_cpp import lines as cpp_lines

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

#: Specifiers of a function head that change neither how a host caller calls it nor its type:
#: dropped from the canonical return type. Anything else in the head (`__global__`,
#: `__device__`, `virtual`, …) stays in it, so the §5.1 comparison sees it as a difference.
LINKAGE_SPECIFIERS = frozenset({"inline", "static", "extern", "__host__", "constexpr",
                                "__inline__", "__forceinline__"})

#: Words that can end a parameter's type and so are never its name.
_TYPE_WORDS = frozenset({
    "void", "bool", "char", "short", "int", "long", "float", "double", "signed", "unsigned",
    "const", "volatile", "auto", "wchar_t", "char16_t", "char32_t", "char8_t", "size_t",
})

_ATTRIBUTE_RE = re.compile(r"\[\[.*?\]\]|__attribute__\s*\(\(.*?\)\)|__launch_bounds__\s*\([^)]*\)"
                           r"|__declspec\s*\([^)]*\)", re.DOTALL)


@dataclass(frozen=True)
class Function:
    name: str
    namespace: tuple[str, ...]
    #: The head before the name, specifiers included, whitespace-normalized.
    head: str
    #: `head` without `LINKAGE_SPECIFIERS` — the return type the §5.1 comparison reads.
    returns: str
    #: `(type, name)` per parameter, type whitespace-normalized; an unnamed one has name "".
    params: tuple[tuple[str, str], ...]
    defined: bool
    line: int


@dataclass(frozen=True)
class Struct:
    name: str
    namespace: tuple[str, ...]
    #: `(type, name)` per data member, in declaration order.
    members: tuple[tuple[str, str], ...]
    line: int


@dataclass(frozen=True)
class Alias:
    name: str
    namespace: tuple[str, ...]
    #: The aliased type, whitespace-normalized (`void(*)(int a, double b)`).
    target: str
    line: int


@dataclass(frozen=True)
class Variable:
    name: str
    namespace: tuple[str, ...]
    #: The whole declaration statement, whitespace-normalized, without its `;`.
    statement: str
    line: int


@dataclass
class Declarations:
    functions: list[Function] = field(default_factory=list)
    structs: list[Struct] = field(default_factory=list)
    aliases: list[Alias] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)
    #: Every namespace opened, as its full path.
    namespaces: set[tuple[str, ...]] = field(default_factory=set)
    #: Structural problems (an unbalanced bracket): the reading stops there.
    errors: list[str] = field(default_factory=list)


def normalize(text: str) -> str:
    """Whitespace-normalized declaration text: runs collapsed to one space, and no space beside
    the punctuation `* & < > , ( ) [ ] :` — so `const std::vector< std::string > &` and
    `const std::vector<std::string>&` are one spelling. Tokens stay apart (`const double`)."""
    text = " ".join(text.split())
    return re.sub(r"\s*([*&<>,()\[\]:])\s*", r"\1", text)


_CLOSE = {"(": ")", "[": "]", "{": "}"}


def _skip_balanced(text: str, i: int) -> int:
    """Index just past the bracket that closes the one at `text[i]`, or -1."""
    stack = [_CLOSE[text[i]]]
    j = i + 1
    while j < len(text):
        ch = text[j]
        if ch in _CLOSE:
            stack.append(_CLOSE[ch])
        elif ch in ")]}":
            if ch != stack[-1]:
                return -1
            stack.pop()
            if not stack:
                return j + 1
        j += 1
    return -1


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """`text` split on `sep` outside `()`, `[]`, `{}` and `<>` (angle brackets counted, which is
    right inside a parameter list or a type, where no comparison operator stands)."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


_PARAM_RE = re.compile(rf"^(?P<type>.*?)(?P<name>{_IDENT})\s*(?P<arr>(?:\[[^\]]*\]\s*)*)$",
                       re.DOTALL)


def parse_param(text: str) -> tuple[str, str]:
    """One parameter as `(normalized type, name)`; `name` is "" when it is unnamed. A default
    argument is dropped."""
    parts = split_top_level(text, "=")
    text = parts[0].strip()
    m = _PARAM_RE.match(text)
    if m and m.group("type").strip() and not m.group("type").rstrip().endswith("::") \
            and m.group("name") not in _TYPE_WORDS:
        return normalize(m.group("type") + m.group("arr")), m.group("name")
    return normalize(text), ""


def parse_params(text: str) -> tuple[tuple[str, str], ...]:
    """A parameter list's contents as `(type, name)` pairs; `()` and `(void)` are empty."""
    if not text.strip() or normalize(text) == "void":
        return ()
    return tuple(parse_param(p) for p in split_top_level(text))


def _strip_attributes(text: str) -> str:
    return _ATTRIBUTE_RE.sub(" ", text)


_NAMESPACE_RE = re.compile(rf"^\s*(?:inline\s+)?namespace\s*(?P<name>{_IDENT}(?:\s*::\s*{_IDENT})*)?\s*$")
_EXTERN_BLOCK_RE = re.compile(r'^\s*extern\s*"\s*"\s*$')
_RECORD_RE = re.compile(rf"^\s*(?P<kw>struct|class|union)\s+(?P<name>{_IDENT})\s*(?:final\s*)?(?::[^{{]*)?$")
_TEMPLATE_RECORD_RE = re.compile(r"^\s*template\s*<.*>\s*(?:struct|class|union)\s", re.DOTALL)
_USING_RE = re.compile(rf"^\s*using\s+(?P<name>{_IDENT})\s*=\s*(?P<target>.+)$", re.DOTALL)
_TYPEDEF_FNPTR_RE = re.compile(rf"^\s*typedef\s+(?P<ret>.+?)\(\s*\*\s*(?P<name>{_IDENT})\s*\)\s*\((?P<params>.*)\)\s*$",
                               re.DOTALL)
_TYPEDEF_RE = re.compile(rf"^\s*typedef\s+(?P<target>.+?)\s*(?P<name>{_IDENT})\s*$", re.DOTALL)


def _function_from(head_and_params: str, qualifiers: str, namespace: tuple[str, ...],
                   defined: bool, line: int) -> Function | None:
    """A function from the statement text up to and including its parameter list, or None when
    the statement is not a function declarator."""
    text = _strip_attributes(head_and_params)
    open_at = text.find("(")
    if open_at < 0 or len(split_top_level(text[:open_at], "=")) > 1:
        return None
    before = text[:open_at].rstrip()
    m = re.search(rf"((?:{_IDENT}\s*::\s*)*~?{_IDENT})$", before)
    if m is None:
        return None
    qualified = re.sub(r"\s+", "", m.group(1))
    head = normalize(before[:m.start()])
    if not head:
        return None  # a call or a constructor-like statement, not a declaration
    close_at = _skip_balanced(text, open_at)
    if close_at < 0:
        return None
    params = parse_params(text[open_at + 1:close_at - 1])
    words = head.split(" ")
    returns = " ".join(w for w in words if w not in LINKAGE_SPECIFIERS)
    trailing = re.search(r"->\s*(.+)$", qualifiers.strip())
    if returns == "auto" and trailing:
        returns = normalize(trailing.group(1))
    name = qualified.split("::")[-1]
    scope = tuple(qualified.split("::")[:-1])
    return Function(name=name, namespace=namespace + scope, head=head, returns=returns,
                    params=params, defined=defined, line=line)


def _members(body: str) -> tuple[tuple[str, str], ...]:
    """The data members of a record body (the text between its braces)."""
    members: list[tuple[str, str]] = []
    i, n = 0, len(body)
    start = 0
    while i < n:
        ch = body[i]
        if ch in "([":
            end = _skip_balanced(body, i)
            if end < 0:
                break
            i = end
            continue
        if ch == "{":
            item = body[start:i]
            end = _skip_balanced(body, i)
            if end < 0:
                break
            if "(" in _strip_attributes(item):
                # A member function (or constructor) body: the item ends here.
                i = end
                while i < n and body[i] in " \t\n;":
                    i += 1
                start = i
                continue
            i = end  # a brace initializer; the member continues to its `;`
            continue
        if ch == ":" and re.fullmatch(r"\s*(?:public|private|protected)\s*", body[start:i]):
            start = i + 1
            i += 1
            continue
        if ch == ";":
            item = _strip_attributes(body[start:i])
            start = i + 1
            i += 1
            stripped = item.strip()
            if not stripped or stripped.startswith(("using ", "typedef ", "friend ", "static ",
                                                    "enum ", "template")):
                continue
            decl = split_top_level(stripped, "=")[0]
            if "(" in decl:
                continue  # a member function declaration
            declarators = split_top_level(decl)
            first_type, first_name = parse_param(re.sub(r"\{.*\}\s*$", "", declarators[0]))
            if first_name:
                members.append((first_type, first_name))
            # `int a, *b;` — each further declarator shares the declaration's base type, and
            # carries its own pointer / reference marks.
            base = first_type.rstrip("*&")
            for extra in declarators[1:]:
                extra_type, extra_name = parse_param(
                    base + " " + re.sub(r"\{.*\}\s*$", "", extra))
                if extra_name:
                    members.append((extra_type, extra_name))
            continue
        i += 1
    return tuple(members)


def read(text: str) -> Declarations:
    """The namespace-scope declarations of `text` (see the module docstring)."""
    code = cpp_lines.strip_preprocessor(cpp_lines.mask(text))
    out = Declarations()
    # One entry per opened block: the namespace names it opens (`namespace a::b {` opens two and
    # is closed by one brace), or `()` for a transparent `extern "C" {` block.
    stack: list[tuple[str, ...]] = []

    def namespace() -> tuple[str, ...]:
        return tuple(name for entry in stack for name in entry)

    i, n = 0, len(code)
    start = 0
    while i < n:
        ch = code[i]
        if ch in "([":
            end = _skip_balanced(code, i)
            if end < 0:
                out.errors.append(f"line {cpp_lines.line_of(code, i)}: unbalanced '{ch}'")
                return out
            i = end
            continue
        if ch == "}":
            if not stack:
                out.errors.append(f"line {cpp_lines.line_of(code, i)}: unbalanced '}}'")
                return out
            stack.pop()
            i += 1
            start = i
            continue
        if ch == ";":
            item = code[start:i]
            _statement(item, namespace(),
                       cpp_lines.line_of(code, start + len(item) - len(item.lstrip())), out)
            i += 1
            start = i
            continue
        if ch == "{":
            item = code[start:i]
            line = cpp_lines.line_of(code, start + len(item) - len(item.lstrip()))
            stripped = _strip_attributes(item).strip()
            ns = _NAMESPACE_RE.match(stripped)
            if ns is not None:
                # An anonymous namespace is the name "".
                stack.append(tuple(re.sub(r"\s+", "", ns.group("name") or "").split("::")))
                out.namespaces.add(namespace())
                i += 1
                start = i
                continue
            if _EXTERN_BLOCK_RE.match(stripped):
                stack.append(())
                i += 1
                start = i
                continue
            end = _skip_balanced(code, i)
            if end < 0:
                out.errors.append(f"line {cpp_lines.line_of(code, i)}: unbalanced '{{'")
                return out
            if stripped.startswith("template"):
                pass  # a template's whole extent is skipped (module docstring)
            elif (record := _RECORD_RE.match(stripped)) is not None:
                out.structs.append(Struct(name=record.group("name"), namespace=namespace(),
                                          members=_members(code[i + 1:end - 1]), line=line))
            elif stripped.startswith("enum") or "(" not in stripped:
                pass  # an enum, or a brace-initialized variable (read at its `;` below)
            else:
                close = stripped.rfind(")")
                fn = _function_from(stripped[:close + 1], stripped[close + 1:], namespace(),
                                    True, line)
                if fn is not None:
                    out.functions.append(fn)
                i = end
                while i < n and code[i] in " \t\n":
                    i += 1
                if i < n and code[i] == ";":
                    i += 1
                start = i
                continue
            i = end
            if (_RECORD_RE.match(stripped) is not None or stripped.startswith("enum")
                    or _TEMPLATE_RECORD_RE.match(stripped) is not None):
                # A record, an enum or a class template ends at its own `;` (after any trailing
                # declarators, which are not read): the next item starts past it.
                semi = code.find(";", i)
                i = n if semi < 0 else semi + 1
                start = i
            elif stripped.startswith("template"):
                # A FUNCTION template's body ends the item with no `;`: the next item starts
                # right here. Reading on to the next `;` swallowed the following statement
                # (found by the round-0 mechanism sweep).
                start = i
            # Otherwise a brace-initialized variable: it continues to its `;`, where it is read.
            continue
        i += 1
    if stack:
        out.errors.append("unbalanced '{': a namespace or block is never closed")
    return out


def _statement(raw: str, namespace: tuple[str, ...], line: int, out: Declarations) -> None:
    """One namespace-scope statement ending in `;` (the `;` excluded)."""
    text = _strip_attributes(raw).strip()
    if not text or text.startswith(("template", "using namespace", "static_assert", "enum",
                                    "friend", "namespace")):
        return
    if (using := _USING_RE.match(text)) is not None:
        out.aliases.append(Alias(name=using.group("name"), namespace=namespace,
                                 target=normalize(using.group("target")), line=line))
        return
    if (typedef := _TYPEDEF_FNPTR_RE.match(text)) is not None:
        out.aliases.append(Alias(
            name=typedef.group("name"), namespace=namespace,
            target=normalize(f"{typedef.group('ret')}(*)({typedef.group('params')})"), line=line))
        return
    if text.startswith("typedef"):
        if (typedef := _TYPEDEF_RE.match(text)) is not None:
            out.aliases.append(Alias(name=typedef.group("name"), namespace=namespace,
                                     target=normalize(typedef.group("target")), line=line))
        return
    if re.match(r"^(?:struct|class|union)\s+" + _IDENT + r"\s*$", text):
        return  # a forward declaration
    before_eq = split_top_level(text, "=")[0]
    if "(" in before_eq and not re.search(r"\{", before_eq):
        close = _last_param_close(text)
        if close > 0:
            fn = _function_from(text[:close], text[close:], namespace, False, line)
            if fn is not None:
                out.functions.append(fn)
                return
    m = re.search(rf"({_IDENT})\s*(?:\[[^\]]*\]\s*)*$", before_eq.strip())
    if m is not None and before_eq.strip()[:m.start()].strip():
        out.variables.append(Variable(name=m.group(1), namespace=namespace,
                                      statement=normalize(text), line=line))


def _last_param_close(text: str) -> int:
    """Index just past the `)` closing the FIRST top-level parenthesis group of `text` (the
    declarator's parameter list, for a function declaration), or -1."""
    open_at = text.find("(")
    if open_at < 0:
        return -1
    return _skip_balanced(text, open_at)
