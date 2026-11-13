"""How the deterministic `Generate` gates read a CUDA C++ source (issue #289, R4-b PR-4).

The `source_reading` capability. Every reader here works on `lines.mask` of the text, so a comment
or the inside of a literal never answers a question about code.

WHAT IS IMPLEMENTED. Every gate a node of this language reaches: the build control file's source
facts (`MODULE_SOURCE_SUFFIXES`, `source_module_deps`), the runner scans
(`validate_runner_json_serialization`, `validate_runner_snapshot_filenames`), the model gates
(`model_source_gates`: the literal-metric floors, the preprocessor allowlist, and on a `problem`
node the three model gates of `run_problem_model_gates`), the loop count the parallel presence
floor reads (`counted_loops`), the absent-model remedy, and — since the host renders a physics
node's runner (issue #289, R4-b PR-6) — the leaf-source isolation gate
(`model_harness_isolation_violations`) and the dependency-use gate
(`validate_dependency_operations`). The C++ binding each implements is stated for a reader in
`docs/backends/language/cuda_cpp/GENERATE_RULES.md`.

A source whose structure `declarations.read` cannot resolve (an unbalanced bracket) is REFUSED by
the gates that read declarations, never passed: a reader that stopped early would report nothing
found, and for a gate that subtracts candidates that is a silent pass.

Imports this package's readers, and through `signatures` the neutral structured-signature form;
nothing else from the neutral core.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

from tools.backends.language.cuda_cpp import declarations as cpp_decls
from tools.backends.language.cuda_cpp import header as cpp_header
from tools.backends.language.cuda_cpp import lines as cpp_lines
from tools.backends.language.cuda_cpp import signatures as cpp_signatures


def code_view(text: str) -> str:
    """`text` with every comment and literal content blanked (`lines.mask`): what counts as CODE
    for a question asked by pattern. The parallel backend's kernel floor asks it through the
    registry."""
    return cpp_lines.mask(text)


# --- the build control file's source facts ------------------------------------------------------

#: The sources the build control file's prerequisite gate reads
#: (`tools/backends/build_system/make/gates.validate_src_dir`).
MODULE_SOURCE_SUFFIXES: tuple[str, ...] = (".cu",)

#: NONE: compiling a CUDA C++ source leaves no module artifact beside its object. The make
#: backend's prerequisite gate reads `None` as "no artifact of that kind", so no prerequisite is
#: taken for one.
MODULE_ARTIFACT_SUFFIX: str | None = None

#: NONE: this reader is regular expressions over the masked source and imports no third-party
#: module, so a launch for a target in this language requires no Python package of its own
#: (`tools/host_prerequisites.required_python_modules`).
REQUIRED_PYTHON_MODULES: tuple[tuple[str, str], ...] = ()


def source_module_deps(src_files: list[Path]) -> dict[str, set[str]]:
    """The object-level dependency edges between `src_files`, by stem: NONE. A source reaches
    another's surface through its host-rendered HEADER (BUNDLE_BINDING.md §1), which compiling
    needs and no object does, so no object of one is a prerequisite of another's compile; the
    objects meet at link."""
    return {path.stem: set() for path in src_files}


# --- the preprocessor surface of a leaf-authored source ---------------------------------------
#
# An ALLOWLIST, not a list of suppression spellings. A leaf-authored source may use exactly two
# directives — `#include "<file>"` / `#include <header>` and `#pragma unroll [<n>]` — and no
# `_Pragma` / `__pragma` operator, no `##` token pasting and no digraph; everything else is refused
# by presence. Round 1 of this change's review measured why a denylist of suppression pragmas
# cannot hold: a line continuation inside the directive, the `%:` digraph for `#`, `_Pragma` with
# its argument on the next line, a `_Pragma` built by `##` pasting, and a GNU linemarker
# (`# 1 "f" 3`, which makes what follows a system header whose warnings are not reported) each
# silenced a lint finding with the denylist green, and `#pragma hd_warning_disable` /
# `nv_exec_check_disable` were two more pragmas it did not list. The same allowlist closes the §5.1
# pin's blind spots on text the compiler never reads (`#if 0`) or reads otherwise (`#define NS
# h_model`, `#define double float`): with no conditional and no macro, the reader and the compiler
# see one program. No flag of the CUDA compiler driver disables an in-source diagnostic pragma,
# which is why this is a source rule and not a lint flag.

# `#pragma unroll` takes any argument (`4`, `(4)`, a constant): an unroll hint cannot control a
# diagnostic. It is a DEVICE-code pragma: in a host function the host compiler reports it unknown,
# which the lint fails.
_ALLOWED_DIRECTIVE_RE = re.compile(
    r'^include[^\S\n]*(?:"[^"\n]*"|<[^>\n]*>)[^\S\n]*$|^pragma[^\S\n]+unroll(?:[^\S\n][^\n]*)?$')
# A directive is a line whose first non-whitespace character is `#` — ANY preprocessing
# whitespace before it: a form feed or a vertical tab there is still a directive to the compiler,
# and round 2 of this change's review silenced lint findings with `\f#pragma` past a `[ \t]*`.
_DIRECTIVE_RE = re.compile(r"^[^\S\n]*#(?P<body>.*)$", re.MULTILINE)
# `<:` is a digraph except in `<::` not followed by `:` or `>` (the lexer's own special case).
_FORBIDDEN_TOKEN_RE = re.compile(r"\b_Pragma\b|\b__pragma\b|##|%:|<%|%>|<:(?!:(?![:>]))|:>")

# A line splice: a backslash before a newline, with or without whitespace between (the compiler
# joins both). Refused ANYWHERE in a leaf source, a comment and a literal included (round 3 of this
# change's review): `/\<newline>*` opens a comment the compiler honours and the one-pass readers of
# `lines` do not see, so a published operation written inside it read as DEFINED to the §5.1 pin
# while the compiler discarded it, and `*\<newline>/` closed one the literal-metric floor read as
# still open. Neither permitted directive needs a continuation.
_SPLICE_RE = re.compile(r"\\[^\S\n]*\n")

# An identifier the language reserves to the implementation: one starting with `_` and an
# upper-case letter, or with `__`. The toolchain's own headers — including the runtime header the
# CUDA compiler driver includes into EVERY source — define macros under such names that expand to
# a diagnostic pragma (round 3 of this change's review: `__NV_SILENCE_DEPRECATION_BEGIN`,
# `_PSTL_PRAGMA(nv_diag_suppress 177)` with no include at all, `_CCCL_DIAG_SUPPRESS_NVCC(177)`
# after one), which silence a lint finding while no directive or `_Pragma` is spelled in the leaf's
# text. So a reserved name is refused unless it is one of the language's own CUDA keywords a kernel
# needs, none of which expands to a pragma (`RealDriverTests` measures the closure).
_RESERVED_IDENTIFIER_RE = re.compile(r"\b(?:_[A-Z]|__)\w*")
RESERVED_IDENTIFIERS_ALLOWED: frozenset[str] = frozenset({
    "__global__", "__device__", "__host__", "__shared__", "__constant__", "__managed__",
    "__restrict__", "__launch_bounds__", "__forceinline__", "__noinline__",
    "__syncthreads", "__syncwarp", "__func__",
})

#: The headers a leaf source may include with `<...>`: the C++17 standard library, and the CUDA
#: runtime header the driver includes anyway. A header outside the toolchain's standard surface may
#: define a suppression macro under an ordinary name (the reserved-identifier rule above covers only
#: reserved ones), so any other `<...>` include is refused; `"..."` names a bundle or host file,
#: which these gates read themselves.
STANDARD_HEADERS: frozenset[str] = frozenset((
    "algorithm any array atomic bitset cassert ccomplex cctype cerrno cfenv cfloat charconv chrono "
    "cinttypes ciso646 climits clocale cmath codecvt complex condition_variable csetjmp csignal "
    "cstdalign cstdarg cstdbool cstddef cstdint cstdio cstdlib cstring ctgmath ctime cuchar cwchar "
    "cwctype deque exception execution filesystem forward_list fstream functional future "
    "initializer_list iomanip ios iosfwd iostream istream iterator limits list locale map memory "
    "memory_resource mutex new numeric optional ostream queue random ratio regex scoped_allocator "
    "set shared_mutex sstream stack stdexcept streambuf string string_view strstream system_error "
    "thread tuple type_traits typeindex typeinfo unordered_map unordered_set utility valarray "
    "variant vector cuda_runtime.h").split())
_ANGLE_INCLUDE_RE = re.compile(r"^include[^\S\n]*<(?P<name>[^>\n]*)>")


def splice_lines(text: str) -> str:
    """`text` with every backslash-newline removed — the translation phase that joins a continued
    line before any directive or token is recognized."""
    return re.sub(r"\\\r?\n", "", text)


def preprocessor_violations(path: Path, text: str) -> list[str]:
    """The allowlist above, over the CODE of `text` after line splicing (a directive or a token
    inside a comment or a literal is not one). Line numbers are those of the spliced text."""
    out: list[str] = []
    for m in _SPLICE_RE.finditer(text):
        out.append(
            f"{path}:{cpp_lines.line_of(text, m.start())}: a backslash at the end of a line is "
            "refused — a leaf-authored source continues no line (not in a comment or a literal "
            "either): a continued comment is read differently by the compiler and by these gates")
    code = cpp_lines.mask(splice_lines(text))
    for m in _DIRECTIVE_RE.finditer(code):
        body = m.group("body").strip()
        include = _ANGLE_INCLUDE_RE.match(body)
        if include and include.group("name").strip() not in STANDARD_HEADERS:
            out.append(
                f"{path}:{cpp_lines.line_of(code, m.start())}: `#include <"
                f"{include.group('name')[:60]}>` is refused — a leaf-authored source includes "
                "only a C++17 standard library header or `cuda_runtime.h` with `<...>`, and "
                "bundle or host files with `\"...\"` (another header may define a macro that "
                "switches the static lint off)")
        elif not _ALLOWED_DIRECTIVE_RE.match(body):
            out.append(
                f"{path}:{cpp_lines.line_of(code, m.start())}: preprocessor directive "
                f"`#{body[:60]}` is refused — a leaf-authored source may use only `#include` and "
                "`#pragma unroll` (no macro, no conditional, no other pragma: a suppression "
                "pragma would switch the static lint off, and a macro or a conditional would make "
                "the §5.1 pin read another program than the compiler builds)")
    for m in _FORBIDDEN_TOKEN_RE.finditer(code):
        out.append(
            f"{path}:{cpp_lines.line_of(code, m.start())}: `{m.group(0)}` is refused — a "
            "leaf-authored source uses no `_Pragma` / `__pragma` operator, no `##` and no "
            "digraph (write `#pragma unroll` as a directive, and spell `#`, `{`, `}`, `[`, `]` "
            "plainly)")
    for m in _RESERVED_IDENTIFIER_RE.finditer(code):
        if m.group(0) not in RESERVED_IDENTIFIERS_ALLOWED:
            out.append(
                f"{path}:{cpp_lines.line_of(code, m.start())}: identifier `{m.group(0)[:60]}` "
                "is refused — a name starting with `_` and a capital letter, or with `__`, is "
                "reserved to the implementation, whose headers define such macros to switch "
                "diagnostics off; a leaf-authored source uses only the CUDA keywords "
                f"{', '.join(f'`{n}`' for n in sorted(RESERVED_IDENTIFIERS_ALLOWED))}")
    return out


def leaf_sources(src_dir: Path) -> list[Path]:
    """Every regular `.cu` file under `src_dir`, at any depth, symbolic links not followed.
    A nested one is read so that the gate below can refuse it."""
    return sorted(p for p in src_dir.rglob("*")
                  if p.is_file() and not p.is_symlink()
                  and p.suffix.lower() in MODULE_SOURCE_SUFFIXES)


# --- the model gates every node gets ------------------------------------------------------------

_CASE_BRANCH_RE = re.compile(r"\bcase_id\s*(?:==|!=|\.\s*(?:compare|find)\s*\()")
_METRIC_INDEX_RE = re.compile(r"\bmetrics\s*\[\s*\d+\s*\]")
_METRIC_ASSIGN_RE = re.compile(r"\bmetrics\s*\[\s*\d+\s*\]\s*=\s*([^;=][^;]*);")
_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def model_source_gates(
    *,
    node_key: str,
    model_file: Path,
    text: str,
    dep_spec_ids: list[str],
    violations: list[str],
    multidim_spec_id: str | None,
    init_member_ids: Iterable[str],
) -> None:
    """The structural gates over one model source.

    Every node: the case-id-keyed metric floor (a `case_id` comparison beside a literal-indexed
    `metrics[...]`), the literal-metric floor (six or more `metrics[<n>] = <literal>`
    assignments), and the preprocessor allowlist (`preprocessor_violations`) — the last over EVERY
    `.cu` under the model's source directory at any depth (`leaf_sources`), because a directive in
    a file the model or runner includes reaches the gates through it — and the rule that every
    `.cu` sits at the top level of that directory. On a `problem` node, the three model gates
    and the member `__init` placement gate (`run_problem_model_gates`; `init_member_ids` the
    `problem` members whose own §5.1 publishes `__init`)."""
    code = cpp_lines.mask(text)
    if _CASE_BRANCH_RE.search(code) and _METRIC_INDEX_RE.search(code):
        violations.append(
            f"{model_file}: hardcoded case_id -> metrics assignment pattern detected")
    assignments = _METRIC_ASSIGN_RE.findall(code)
    literal_like = sum(1 for rhs in assignments if _NUMBER_RE.search(rhs))
    if len(assignments) >= 6 and literal_like >= 6:
        violations.append(
            f"{model_file}: many literal metric assignments detected "
            f"({literal_like}/{len(assignments)})")
    # On a physics node the runner is HOST-rendered (`runner.render_runner`) — it is not held to
    # the leaf's allowlist, and it uses what the allowlist refuses a leaf (`std::_Exit`, a
    # reserved name). Round 4 of this change's review found every physics node's Generate.gate
    # refused on that host file, which no leaf can edit. On an `infrastructure` node the leaf
    # authors the runner, and it is read like any other leaf source.
    host_runner = (model_file.parent / (model_file.name.removesuffix("_model.cu") + "_runner.cu")
                   if not node_key.startswith("infrastructure/") else None)
    for source in leaf_sources(model_file.parent):
        if source == host_runner:
            continue
        source_text = (text if source == model_file
                       else source.read_text(encoding="utf-8", errors="ignore"))
        violations.extend(preprocessor_violations(source, source_text))
        if source.parent != model_file.parent:
            # Build compiles every `.cu` as its own object, and the syntax stage compiles only
            # the top level (the compiler tool takes no path below its directory): a nested one
            # would reach Build uncompiled for the target, or — included from a top-level file
            # as well — be compiled twice (round 3 of this change's review). Since issue #420 the
            # syntax stage refuses a nested `.cu` itself, and the static stage runs only after
            # it passes, so inside `Generate.gate` this refusal is a second statement of the
            # same rule, kept for a caller that runs the static gates alone.
            violations.append(
                f"{source}: a CUDA C++ source in a subdirectory is refused — every `.cu` is a "
                "translation unit of its own and sits beside the host-rendered header at the top "
                "of the source directory (move it there, and do not `#include` a `.cu`)")
    run_problem_model_gates(node_key, model_file, text, dep_spec_ids, violations,
                            multidim_spec_id=multidim_spec_id, init_member_ids=init_member_ids)


def model_source_not_found_violation(
    src_dir: Path, expected_model_name: str | None, model_glob: str
) -> str:
    """The violation for an absent node model source: generic when the name is unknown, a rename
    instruction when a model source exists under another name, "not found" otherwise."""
    if expected_model_name is None:
        return f"{src_dir}: model source not found"
    present = sorted(p.name for p in src_dir.glob(model_glob) if p.is_file())
    if present:
        return (f"{src_dir}: model source {', '.join(present)} present but must be named "
                f"{expected_model_name} (literal spec_id prefix required; abbreviated/derived "
                "prefix rejected) — rename to match")
    return f"{src_dir}: node model source not found ({expected_model_name})"


# --- the runner scans ---------------------------------------------------------------------------

# A printf-family conversion inside a format literal. Only the conversion character and its
# precision are read.
_CONVERSION_RE = re.compile(r"%[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?(?:hh|h|ll|l|L|j|z|t)?([a-z%])")
# A printf-family call (`printf`, `fprintf`, `snprintf`, `vsnprintf`, ...): only a literal among
# its arguments is a FORMAT — `puts("100% accurate")` is text (round 2 of this change's review
# found the scan refusing it).
_PRINTF_CALL_RE = re.compile(r"\b(?:v?f|v?s|v?sn|v)?printf\s*\(")


def _close_paren(code: str, open_at: int) -> int:
    """Offset of the `)` closing the `(` at `open_at` in masked `code`, or the end."""
    return _close_bracket(code, open_at, "(", ")")


def _close_bracket(code: str, open_at: int, opener: str, closer: str) -> int:
    """Offset of the ``closer`` closing the ``opener`` at `open_at` in masked `code`, or the end."""
    depth = 0
    for index in range(open_at, len(code)):
        if code[index] == opener:
            depth += 1
        elif code[index] == closer:
            depth -= 1
            if depth == 0:
                return index
    return len(code)


#: What may stand between a lambda's `]` (or its `)`) and its `{`: `mutable`, `constexpr`,
#: `noexcept`, `-> type`. Anything else — a `;`, an `=`, a `}` — means the `[` opened no lambda.
_LAMBDA_TAIL_RE = re.compile(r"[\w\s:<>,*&()\->]*")
#: A lambda introducer BOUND to a name: `NAME = [`. Whether the statement DECLARES that name is
#: `_LAMBDA_DECLARATION_HEAD_RE`'s question (a member `s.f = [` is not a declaration).
_LAMBDA_BINDING_RE = re.compile(r"(?<!\w)(?P<name>[A-Za-z_]\w*)\s*=\s*\[")
#: An init-capture (`[v = expr]`, `[&r = out]`) or anything that may evaluate code in the
#: capture list: its initializer runs when the lambda is CREATED, so the lambda is live.
_LAMBDA_EVALUATING_CAPTURE_RE = re.compile(r"[({]|\w\s*=")
#: What a statement that DECLARES the bound name says before it: `auto` or `std::function<…>`,
#: optionally after `static` / `const` / `constexpr`. Anything else — nothing (an assignment to a name declared elsewhere: a
#: parameter, a range-for variable, a namespace-scope variable, all invocable without this body
#: writing the name), `for (auto`, a declarator list — is not read as a declaration.
_LAMBDA_DECLARATION_HEAD_RE = re.compile(
    r"\s*(?:(?:const|constexpr|static)\s+)*(?:auto|std\s*::\s*function\s*<[^;{}=]*>)\s*")


def _blank_dead_lambdas(body: str) -> str:
    """``body`` with every lambda bound to a name the body never uses again blanked IN PLACE,
    from the start of its declaration statement through its `;` (issue #506).

    The `problem` model gates read a function's body flow-insensitively, so an assignment inside
    a lambda that is never invoked — `auto dead = [&]() { out = fv; };` — fed the dataflow
    closure exactly as a live one did and silenced the gate (measured at `c9f46eee`: 0
    violations). This is the C++ form of the Fortran binding's never-named contained procedure
    (`fortran/source.py` `_dead_contained_spans`), and it is read the same way: by whether the
    NAME occurs, not by enumerating how it is invoked.

    * A BOUND lambda is `NAME = [` (`_LAMBDA_BINDING_RE`) whose `[` is followed by its balanced
      `]`, an optional `(params)`, an optional tail (`_LAMBDA_TAIL_RE`) and a `{`. A subscript
      `a[i]`, an array declarator `double buf[4]{…}` and `new int[3]{}` have no `=` right before
      their `[`; `x = a[0];` has no `{` after its `]`. An unbound lambda — passed as an
      argument, invoked in place, returned — is live.
    * Only a statement that DECLARES the name can make it dead: the text from the statement's
      start to NAME must be `auto` or `std::function<…>`, optionally after `static` / `const` /
      `constexpr` (`_LAMBDA_DECLARATION_HEAD_RE`). A plain `NAME = [` assigns a name declared elsewhere — a
      reference parameter, a range-for variable, a namespace-scope variable — that can be
      invoked without this body writing it, and `for (auto f = [` and `T f, &g = f = [` are not
      a declaration of one name; all of these are live (round 1 of issue #506's review: each
      compiled and was refused). Declarations in any other spelling stay live too:
      `[[maybe_unused]] auto f = [`, `auto f{[…]{…}};`, `const auto& f = [`, `auto&& f = [`, a
      type alias `Fn f = [` — those are read as they were before this function existed.
    * A lambda with an init-capture (`[v = expr]`) is live: the initializer runs when the
      lambda is created, whether or not it is ever invoked (round 2, Codex).
    * A bound one is DEAD when `\bNAME\b` occurs nowhere in the body outside its own statement,
      and when its `}` is followed by `;`. Otherwise it is live, which is how every lambda was
      read before this.

    Dead text is removed and the scan repeated to a fixed point, so a lambda named only inside
    a dead one is dead too, and a lambda nested in a dead one goes with it. Blanking keeps every
    newline and every offset, the invariant the dataflow gate's `pos < call_start` comparison
    rests on.

    NOT CLOSED, and the same bound as the Fortran binding's (its dataflow gate's residue (d)):
    a dead branch (`if (false) { out = fv; }`), every spelling that names the dead lambda —
    `(void)dead;`, a two-statement `std::function<void()> f; f = [&]{…};` (the declaration names
    `f`) — and every declaration spelling the bullets above leave live. The gates do not read
    control flow.
    """
    while True:
        blanked = _blank_dead_lambdas_once(body)
        if blanked == body:
            return body
        body = blanked


def _blank_dead_lambdas_once(body: str) -> str:
    spans: list[tuple[int, int]] = []
    index = 0
    while True:
        binding = _LAMBDA_BINDING_RE.search(body, index)
        if binding is None:
            break
        open_at = binding.end() - 1
        index = open_at + 1
        close_at = _close_bracket(body, open_at, "[", "]")
        if _LAMBDA_EVALUATING_CAPTURE_RE.search(body, open_at + 1, close_at):
            continue
        cursor = close_at + 1
        rest = body[cursor:].lstrip()
        if rest.startswith("("):
            cursor = _close_paren(body, body.index("(", cursor)) + 1
        tail = _LAMBDA_TAIL_RE.match(body, cursor)
        brace_at = tail.end() if tail else cursor
        if brace_at >= len(body) or body[brace_at] != "{":
            continue
        end_at = _close_bracket(body, brace_at, "{", "}")
        semicolon = re.compile(r"\s*;").match(body, end_at + 1)
        if end_at >= len(body) or semicolon is None:
            continue
        statement_start = max(body.rfind(ch, 0, binding.start()) for ch in ";{}") + 1
        if not _LAMBDA_DECLARATION_HEAD_RE.fullmatch(body, statement_start, binding.start()):
            continue
        statement_end = semicolon.end()
        outside = body[:statement_start] + body[statement_end:]
        if re.search(rf"\b{re.escape(binding.group('name'))}\b", outside):
            continue
        spans.append((statement_start, statement_end))
        index = statement_end
    if not spans:
        return body
    characters = list(body)
    for start, end in spans:
        for position in range(start, end):
            if characters[position] != "\n":
                characters[position] = " "
    return "".join(characters)


def validate_runner_json_serialization(
    runner_file: Path,
    text: str,
    violations: list[str],
) -> None:
    """Flag the output spellings that cannot produce a JSON token, by their presence in the
    runner (`docs/backends/language/cuda_cpp/RUNNER_OUTPUT.md` §1): a `%a` hexadecimal
    floating-point conversion in a format literal, and the `std::hexfloat` stream manipulator.
    `text` arrives lowercased from the validator, so `%A` reads as `%a`. Descriptor-syntactic like
    the Fortran scan: a runtime fixup does not pass, and a spelling this does not list is the
    runtime deliverable gate's (every runner document must parse as JSON). A preprocessor
    directive in a LEAF-authored runner (an `infrastructure` node's) is judged by
    `model_source_gates`; a physics node's runner is host-rendered and not held to that allowlist."""
    code = cpp_lines.mask(text)
    spans = cpp_lines.literal_spans(text)
    formats: list[tuple[int, str]] = []
    for call in _PRINTF_CALL_RE.finditer(code):
        open_at = call.end() - 1
        close_at = _close_paren(code, open_at)
        inside = [span for span in spans if open_at < span[0] < close_at]
        # The FORMAT is the call's first literal, with the literals concatenated onto it; a later
        # literal is an argument (`printf("%s", "100% Accurate")` prints text — round 3 of this
        # change's review found the scan refusing it).
        end = None
        for start, stop, literal in inside:
            if end is not None and code[end:start].strip():
                break
            formats.append((start, literal))
            end = stop
    for start, literal in formats:
        lineno = cpp_lines.line_of(text, start)
        for m in _CONVERSION_RE.finditer(literal):
            if m.group(1) == "a":
                violations.append(
                    f"{runner_file}:{lineno}: forbidden JSON output format `{m.group(0)}` — a "
                    "hexadecimal floating-point conversion is not a JSON number; write a real "
                    "with `%.16e` (RUNNER_OUTPUT.md §1)")
    for m in re.finditer(r"\bhexfloat\b", code):
        violations.append(
            f"{runner_file}:{cpp_lines.line_of(code, m.start())}: forbidden JSON output "
            "manipulator `std::hexfloat` — hexadecimal floating point is not a JSON number")


_SNAPSHOT_LITERAL_RE = re.compile(r"state_snapshots/([^/'\"]+)\.json")
_FILE_OPEN_RE = re.compile(r"\bofstream\b|\bfopen\s*\(|\.\s*open\s*\(|\bfreopen\s*\(")


def validate_runner_snapshot_filenames(
    runner_file: Path,
    text: str,
    violations: list[str],
    known_case_ids: set[str] | None = None,
) -> None:
    """Flag a hardcoded `raw/state_snapshots/<name>.json` literal on a line that opens a file:
    the runner must build each snapshot's name from the case id it received on argv. A literal
    whose stem is a declared case id, and `snapshot_schema`, are exempt (the Fortran scan's
    rules; `text` is lowercased by the validator)."""
    case_id_stems = {cid.lower() for cid in known_case_ids} if known_case_ids else set()
    code_lines = cpp_lines.mask(text).split("\n")
    for lineno, literal in cpp_lines.literals(text):
        if "state_snapshots/" not in literal:
            continue
        if not _FILE_OPEN_RE.search(code_lines[lineno - 1]):
            continue
        for match in _SNAPSHOT_LITERAL_RE.finditer(literal):
            name = match.group(1)
            if name == "snapshot_schema" or name in case_id_stems:
                continue
            violations.append(
                f"{runner_file}:{lineno}: hardcoded snapshot filename "
                f"'state_snapshots/{name}.json' — write one raw/state_snapshots/<case_id>.json "
                "per case, building the name from the case_id received on argv "
                "(`\"raw/state_snapshots/\" + case_id + \".json\"`); a fixed/sequential name "
                "fails Validate.execute's per-case deliverable gate")


# --- the parallel presence floor's loop count ---------------------------------------------------

_FOR_RE = re.compile(r"\bfor\s*\(")


def counted_loops(text: str) -> int:
    """The number of counted `for` loops in the CODE of `text`: a `for (init; cond; step)` header
    (two top-level `;`), not a range-based `for (x : range)`."""
    code = cpp_lines.mask(text)
    count = 0
    for m in _FOR_RE.finditer(code):
        open_at = m.end() - 1
        depth = 0
        semis = 0
        for ch in code[open_at:]:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            elif ch == ";" and depth == 1:
                semis += 1
        if semis == 2:
            count += 1
    return count


# --- the leaf-source isolation gate (a physics node) --------------------------------------------

_INCLUDE_RE = re.compile(r'^include[^\S\n]*"(?P<name>[^"\n]*)"')


def quoted_includes(text: str) -> list[tuple[int, str]]:
    """Every `#include "<name>"` directive of `text` that is CODE — not inside a comment — as
    `(line number, name)`. The directive is found on the masked, spliced text (so a commented-out
    one is not seen) and its name is read from the same span of the spliced original (the mask
    blanks a literal's contents, and the quoted name is one)."""
    spliced = splice_lines(text)
    code = cpp_lines.mask(spliced)
    out: list[tuple[int, str]] = []
    for m in _DIRECTIVE_RE.finditer(code):
        body = spliced[m.start("body"):m.end("body")].strip()
        include = _INCLUDE_RE.match(body)
        if include is not None:
            out.append((cpp_lines.line_of(code, m.start()), include.group("name").strip()))
    return out


def _structure_refusal(path: Path, errors: list[str], gate: str) -> str:
    return (f"{path}: {gate} cannot read this source's declarations ({'; '.join(errors)}) — a "
            "bracket that does not balance ends the reading, and a gate that read half a source "
            "would pass the rest unread; balance every `(`, `[` and `{` (a bracket inside a "
            "comment or a literal is not counted)")


# Any mention of a harness's namespace or operations in code — a qualified name, a
# `namespace h = harness_<x>_model;` alias, a `using` of it (round 1 of this change's review: the
# alias passed a `::`-anchored pattern).
_HARNESS_REFERENCE_RE = re.compile(r"\bharness_\w*_model\b|\bharness_\w+__\w+")

#: Names that open, rename, delete or truncate a file, run a command, register an exit handler or
#: embed assembly, and mean nothing else: refused as a TOKEN anywhere in a leaf source's code,
#: whether called, taken by address or bound to a pointer (round 3 of this change's review
#: reached `fopen` / `freopen` through a function pointer and a lambda, past a scan that wanted
#: the name followed by `(`).
LEAF_IO_NAMES: tuple[str, ...] = (
    "fopen", "fopen64", "freopen", "freopen64", "fdopen", "popen", "creat", "creat64",
    "renameat", "renameat2", "unlink", "unlinkat", "ftruncate", "ftruncate64", "truncate64",
    "open64", "openat", "openat64", "execl", "execlp", "execle", "execv", "execvp", "execvpe",
    "execve", "atexit", "at_quick_exit", "asm", "syscall", "posix_spawn", "posix_spawnp",
    "fork", "vfork", "mkstemp", "mkostemp", "symlink", "symlinkat", "linkat", "dup2", "dup3",
)
#: Names that ALSO name ordinary things (a physics helper `open_boundary`, a local `system`): refused
#: when called, qualified (`std::rename`) or taken by address — never as a bare word.
LEAF_IO_CALL_NAMES: tuple[str, ...] = ("open", "rename", "system", "truncate", "link")

#: What a leaf-authored source of a physics node may not contain: every file-stream and
#: stream-buffer class (`ofstream`, `fstream`, `basic_filebuf<char>`, the wide ones), anything of
#: `std::filesystem`, the names above, and the one-path `remove` (the three-argument algorithm of
#: `<algorithm>` is not a file operation). Why every leaf source (round 2 of this change's review):
#: a C++ program runs namespace-scope destructors and `atexit`
#: handlers after `main` returns — after the harness has written the run's outputs. The
#: host-rendered runner now ends with `std::_Exit`, so no such code runs (round 3); this refusal is
#: the second layer, and it also covers I/O from inside an operation. Emission is the harness's alone.
#: A language-linkage specification (`extern "C" long syscall(long, ...);`): it declares a C symbol
#: no allowed header provides, which is how round 5 of this change's review reached a system call
#: past every name above. A leaf source has no reason to declare one. The literal's contents are
#: masked, its quotes are not.
_LINKAGE_SPEC_RE = re.compile(r'\bextern\s*"')
_LEAF_IO_RE = re.compile(
    r"\b\w*fstream\b|\b\w*filebuf\b|\bfilesystem\b"
    r"|\b(?:" + "|".join(LEAF_IO_NAMES) + r")\b"
    r"|(?:\bstd\s*::\s*|(?<![\w\s])\s*::\s*|&\s*)(?:" + "|".join(LEAF_IO_CALL_NAMES) + r")\b"
    r"|\b(?:" + "|".join(LEAF_IO_CALL_NAMES) + r")\s*\("
    r"|\bremove\s*\(\s*[^,()]*\)")


_EAST_CONST_RE = re.compile(r"^(?P<base>[^&*]+?) const(?P<ref>[&*]?)$")


def _canonical_type(ctype: str) -> str:
    """A normalized parameter type with an east `const` moved west (`std::string const&` ->
    `const std::string&`): one type, two spellings."""
    east = _EAST_CONST_RE.match(ctype)
    return f"const {east.group('base')}{east.group('ref')}" if east else ctype


def model_harness_isolation_violations(
    src_dir: Path, model_files: list[Path], spec_id: str,
    *, allowed_harness_uses: Any = None, forbidden_outputs: Iterable[str] = (),
) -> list[str]:
    """The isolation gate over a physics node's leaf-authored sources (every `.cu` under `src_dir`
    but the host-rendered runner, by its full path): none includes a harness header or names the
    harness (`harness_<x>_model`, `harness_<x>__<op>`) — the host-rendered runner is the only
    caller of the harness — and none does file I/O, runs a command, or registers code to run
    after `main` (`_LEAF_IO_RE`), or holds a string literal naming a run output
    (`forbidden_outputs`, the file names only the host writes). `allowed_harness_uses` is accepted
    for the neutral caller's sake and must be empty: no harness of this language publishes an
    operation for a physics source (the Fortran reader's docstring says what it admits)."""
    if allowed_harness_uses:
        raise ValueError("no harness of this language admits a physics-source use")
    violations: list[str] = []
    runner = src_dir / f"{spec_id}_runner.cu"
    others = [p for p in leaf_sources(src_dir) if p != runner and p not in model_files]
    for path in [*model_files, *others]:
        source = path.read_text(encoding="utf-8", errors="ignore")
        harness_includes = [name for _line, name in quoted_includes(source)
                            if Path(name).name.startswith("harness_")]
        code = cpp_lines.strip_preprocessor(cpp_lines.mask(splice_lines(source)))
        if harness_includes or _HARNESS_REFERENCE_RE.search(code):
            violations.append(
                f"{path}: a physics source must not include or name the harness — the physics "
                "node never depends on the harness at the source level (the host-rendered "
                "runner is the sole caller of the harness)")
        if _LINKAGE_SPEC_RE.search(code):
            violations.append(
                f"{path}: a physics source must not declare a language linkage (`extern \"C\"`) — "
                "it would declare a C symbol no allowed header provides; call only the standard "
                "library, the CUDA runtime and the host-rendered headers")
        io = _LEAF_IO_RE.search(code)
        if io:
            violations.append(
                f"{path}: a physics source must not do file I/O, run a command, or register code "
                f"to run after `main` (`{io.group(0).strip()}` — no file stream, `fopen` / `open`, "
                "`std::filesystem`, `rename` / `unlink` / a one-path `remove`, `system` / `popen` "
                "/ `exec*`, `atexit`) — emission is the harness's job alone, through the "
                "host-rendered driver; the model only computes")
        contents = [literal.lower() for _line, literal in cpp_lines.literals(source)]
        violations.extend(
            f"{path}: forbidden judge-artifact filename detected ({output_name})"
            for output_name in forbidden_outputs
            if any(output_name in literal for literal in contents))
    return violations


def published_subroutines(text: str, spec_id: str) -> list[str]:
    """The distinct, source-ordered `<spec_id>__`-named functions `text` DEFINES in namespace
    `<spec_id>_model` — a component's published operation surface."""
    namespace = (f"{spec_id}_model",)
    out: list[str] = []
    for fn in cpp_decls.read(text).functions:
        if (fn.defined and fn.namespace == namespace and fn.name.startswith(f"{spec_id}__")
                and fn.name not in out):
            out.append(fn.name)
    return out


def published_operation_missing(name: str) -> str:
    return (f"generated model source does not publish component public_api operation '{name}' "
            f"— define `{name}(...)` in the model's namespace (the IR public_api pins it as a "
            "published operation)")


def published_operation_extra(spec_id: str, name: str) -> str:
    return (f"generated model source publishes `{spec_id}__` function '{name}' that is NOT in "
            "the IR public_api.published_operations — a component's published surface must match "
            "its IR public_api exactly (rename an internal helper without the "
            f"`{spec_id}__` prefix, or add it to the published operations)")


# --- the dependency-use gate -------------------------------------------------------------------

def _dependency_op_call_re(spec_id: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(spec_id)}__\w+\s*\(")


def validate_dependency_operations(
    model_files: list[Path],
    dep_spec_ids: list[str],
    violations: list[str],
) -> None:
    """Each model source uses each direct dependency the way the build links it: it includes the
    dependency's host-rendered header (`"<dep>_model.cuh"`), it defines no `<dep>__*` function
    (a redefinition of the dependency's operation), and it calls at least one `<dep>__*`
    operation from a function body (qualified `<dep>_model::` or not) outside a dead named
    lambda (`_blank_dead_lambdas`). The three clauses of the
    Fortran binding's `use` / `subroutine` / `call` check, read over CODE only (`lines.mask`), so
    a comment or a literal answers none of them. A source whose declarations do not read is
    refused rather than judged on the part that was read. A node with no dependency — the harness —
    has nothing to check."""
    if not dep_spec_ids:
        return
    for model_file in model_files:
        text = model_file.read_text(encoding="utf-8", errors="ignore")
        includes = {Path(name).name for _line, name in quoted_includes(text)}
        decls = cpp_decls.read(text)
        if decls.errors:
            violations.append(_structure_refusal(model_file, decls.errors,
                                                 "the dependency-use gate"))
            continue
        # A call in a dead named lambda is not a use (issue #506): every gate reads dead code as
        # absent, this one included.
        bodies = [_blank_dead_lambdas(fn.body) for fn in decls.functions if fn.defined]
        for spec_id in dep_spec_ids:
            if cpp_header.basename(spec_id) not in includes:
                violations.append(
                    f"{model_file}: missing dependency header include "
                    f"(#include \"{cpp_header.basename(spec_id)}\")")
            if any(fn.defined and fn.name.startswith(f"{spec_id}__") for fn in decls.functions):
                violations.append(
                    f"{model_file}: dependency operation redefinition detected ({spec_id}__*)")
            call_re = _dependency_op_call_re(spec_id)
            if not any(call_re.search(body) for body in bodies):
                violations.append(
                    f"{model_file}: {missing_dependency_call_message(spec_id)}")


def missing_dependency_call_message(spec_id: str) -> str:
    """The dependency-use gate's refusal for a source that calls no `<spec_id>__*` operation.
    It names what IS read, because the C++ binding has no import rename to follow (issue #534
    PR-J3-a): a call by the operation's name from a function body. A pointer alias
    (`auto f = &dep__op; f(x)`) and a reference alias (`auto& f = dep__op;`) are refused; a
    lambda whose body calls `dep__op(...)` passes, because the call is in a live body — the three
    answers `test_language_cuda_cpp` pins as measured."""
    return (f"missing dependency operation call ({spec_id}__*): a call of "
            f"`{spec_id}__<op>(...)` by that name, from a function body; an alias through a "
            "pointer or reference is not read")


# --- the three `problem` model gates -----------------------------------------------------------
#
# The C++ reading of the Fortran binding's gates (`tools/backends/language/fortran/source.py`
# `run_problem_model_gates`), over `declarations.read`'s namespace-scope function definitions. A
# parameter is an OUTPUT when the function can write through it: a non-const reference, a pointer
# to non-const, or a non-const `spec2science::View`; everything else is an input. A function returning
# a value has that value as one more output (the Fortran function result), read off its `return`
# statements. An assignment is `<name> [index...] op= <expr>;` over the masked body, with the
# name the base of the target (`u[i] = ...`, `u.data[i] = ...`).

_OUT_VIEW_RE = re.compile(r"^(?:spec2science::)?View<(?P<element>.+),\d+>$")
_ARRAY_TYPE_RE = re.compile(r"\b(?:View|Array|vector)\s*<|\*$")
_ASSIGNMENT_HEAD_RE = re.compile(
    r"(?<![\w.>:])(?P<lhs>[A-Za-z_]\w*)\s*"
    r"(?P<index>(?:\[[^\];]*\]\s*|\.\s*data\s*(?:\(\s*\))?\s*\[[^\];]*\]\s*"
    r"|\.\s*data\s*(?=[-+*/%&|^]?=(?!=)))*)"
    r"(?P<op>[-+*/%&|^]?=)(?!=)")


class _Assignment(NamedTuple):
    lhs: str
    index: str
    op: str
    rhs: str
    start: int


def _assignment_matches(body: str) -> list[_Assignment]:
    """Every `lhs [index] op= rhs` of masked `body`. The right-hand side ends at the first `;` at
    its own bracket depth, or at a `)` that closes a bracket opened BEFORE the `=` — so the
    step of a `for` header (`i += stride)`) does not swallow the loop body after it, braced or not
    (round 5 of this change's review: a grid-stride loop's body was lost, and a discarded result
    reached the output through the swallowed text). Heads are found independently of each other,
    so an assignment inside a loop body is found after the header's. A top-level `,` does not end
    it (a template argument list `View<double, 1>` has one, and `<` cannot be told from a
    comparison here), so the first declarator of `double a = x, b = y;` also takes `b`, `y` as
    sources — an extra edge, never a lost one."""
    out: list[_Assignment] = []
    for m in _ASSIGNMENT_HEAD_RE.finditer(body):
        depth = 0
        end = m.end()
        while end < len(body):
            ch = body[end]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif ch == ";" and depth == 0:
                break
            end += 1
        out.append(_Assignment(m.group("lhs"), m.group("index"), m.group("op"),
                               body[m.end():end], m.start()))
    return out
_RETURN_RE = re.compile(r"\breturn\b(?P<expr>[^;]*);")
_LOOP_RE = re.compile(r"\bfor\s*\(|\bwhile\s*\(|<<<")
_VIEW_DECLARATION_RE = re.compile(
    r"\bView\s*<[^;{}]*>\s+(?P<name>[A-Za-z_]\w*)\s*(?:=\s*)?[{(](?P<init>[^;]*);")
_STORAGE_NAME_RE = re.compile(r"(?<![\w.>:])([A-Za-z_]\w*)\s*(?:\.\s*data\b|\[)")
_IDENTIFIER_TOKEN_RE = re.compile(r"[A-Za-z_]\w*")
# A numeric literal token — digits with separators, a fraction, an exponent and the C++ suffixes
# — matched as a WHOLE token, so a name made only of suffix letters (`e`, `f`, `ul`) is a name,
# not a literal (round 1 of this change's review: `energy = e;` read as literal-only).
_NUMBER_TOKEN_RE = re.compile(
    r"(?<![\w.])(?:\d[\d']*\.?[\d']*|\.\d[\d']*)(?:[eE][+-]?\d[\d']*)?[fFlLuU]*(?![\w.])")
_LITERAL_OPERATORS_RE = re.compile(r"[+\-*/()\s,]*")
_CONST_DECLARATION_RE = re.compile(
    r"\b(?:constexpr|const)\b[^;(){}=]*?\b(?P<name>[A-Za-z_]\w*)\s*(?:=|\{)")


def is_output_parameter(ctype: str) -> bool:
    """Whether a function can write through a parameter of (normalized) type `ctype`. A
    `__restrict__` qualifier and a top-level `const` on a by-value view or pointer say nothing
    about the pointee, and an east `const` is the west one (round 3 of this change's review: a
    kernel's `double* __restrict__ out` was read as an input)."""
    ctype = re.sub(r"\b__restrict__\b|\b__restrict\b", "", ctype).strip()
    ctype = re.sub(r"(?<=[>*])\s*const$", "", ctype)
    ctype = _canonical_type(ctype)
    if ctype.startswith(("const ", "const&")):
        return False
    view = _OUT_VIEW_RE.match(ctype)
    if view is not None:
        return not view.group("element").startswith("const ")
    return ctype.endswith(("&", "*"))


def _identifiers(expr: str) -> set[str]:
    return {tok for tok in _IDENTIFIER_TOKEN_RE.findall(expr)
            if tok not in cpp_signatures.CPP_KEYWORDS}


def _is_literal_like(expr: str) -> bool:
    """`expr` is a boolean literal, or numeric literals joined by arithmetic and parentheses."""
    expr = expr.strip()
    if expr in ("true", "false"):
        return True
    if not _NUMBER_TOKEN_RE.search(expr):
        return False
    return _LITERAL_OPERATORS_RE.fullmatch(_NUMBER_TOKEN_RE.sub(" ", expr)) is not None


#: Words that open a statement without declaring anything, so an assignment after one is a plain
#: assignment (`else x = 1;`), not a declaration's initializer.
_STATEMENT_KEYWORDS = frozenset({"else", "do", "return", "case", "default"})


def _is_declaration(body: str, lhs_at: int) -> bool:
    """Whether the assignment whose target starts at `lhs_at` is a DECLARATION's initializer
    (`std::vector<double> f = ...;`, `auto f = ...;`, `for (int i = 0; ...)`, the second
    declarator of `double a = 0.0, b = 1.0;`): something other than an operator stands between
    the statement's start — the last `;`, `{`, `}`, `(` or `)` — and the target."""
    start = max(body.rfind(ch, 0, lhs_at) for ch in ";{}()") + 1
    prefix = body[start:lhs_at].strip()
    return (bool(prefix) and not prefix.endswith(("=", "?", ":"))
            and prefix.split()[0] not in _STATEMENT_KEYWORDS)


#: The null pointer constants an assignment resets a pointer to.
_NULL_POINTERS = frozenset({"nullptr", "NULL"})


def _assignments(body: str) -> list[tuple[str, set[str], int, str, bool]]:
    """`(target name, source identifiers, offset, right-hand text, not an assignment statement)`
    per data edge of `body`.

    An assignment `lhs op= rhs;` makes every `rhs` identifier a source of `lhs` (a compound `op=`
    also reads `lhs`, an edge the backward closure gains nothing from). A declaration's
    initializer carries data
    the same way, and is flagged: the dataflow gate's "assigned before the call" clause reads
    assignment STATEMENTS only, as the Fortran binding's does (its pattern matches no
    declaration). An ALIAS record is added for every name that is made to point into another's
    storage — a view or pointer built over `u.data()` / `&u[...]` (`View<double, 1> v{u.data(),
    ...}`, `auto v = View<...>{u.data(), ...}`, `double* p = u.data();`, `v.data = u.data();`):
    a write through the alias is a write to `u`, so `u` takes the alias as a source."""
    records: list[tuple[str, set[str], int, str, bool]] = []
    for m in _assignment_matches(body):
        lhs, rhs = m.lhs, m.rhs
        sources = _identifiers(rhs)
        pointees = _pointee_names(rhs) - {lhs}
        # Making `lhs` point into another's storage sets up where a call will WRITE, not a value
        # the call reads, so it is not an assignment statement for the "assigned before" clause;
        # nor is resetting a pointer to null (round 1 of #380 PR-2's review: growing a kept
        # buffer, `cudaFree(g_f); g_f = nullptr; cudaMalloc(&g_f, n);`, dropped the buffer a
        # kernel then writes the dependency's result into). Another spelling of the reset
        # (`g_f = 0;`, `g_f = alloc(n);`) is still an assignment statement.
        records.append((lhs, sources, m.start, rhs.strip(),
                        bool(pointees) or rhs.strip() in _NULL_POINTERS
                        or _is_declaration(body, m.start)))
        for storage in pointees:
            records.append((storage, {lhs}, m.start, "", True))
    for m in _VIEW_DECLARATION_RE.finditer(body):
        for storage in _pointee_names(m.group("init")):
            records.append((storage, {m.group("name")}, m.start(), "", True))
    return records


_POINTEE_RE = re.compile(r"(?<![\w.>:])([A-Za-z_]\w*)\s*\.\s*data\b|(?<![\w)\]&])&\s*([A-Za-z_]\w*)")


def _pointee_names(expr: str) -> set[str]:
    """The names whose storage `expr` POINTS into — `u.data` / `u.data()`, `&u` / `&u[i]` — as
    opposed to a value read out of it (`u[i]`), which aliases nothing."""
    return {a or b for a, b in _POINTEE_RE.findall(expr)}


def _storage_names(expr: str) -> set[str]:
    """The names whose storage `expr` points into: `u.data`, `u[...]`, `&u`."""
    return (set(_STORAGE_NAME_RE.findall(expr))
            | set(re.findall(r"(?<![\w)\]&])&\s*([A-Za-z_]\w*)", expr)))


#: A callee's summary: for each output parameter position, the input positions whose data can
#: reach it. The CUDA runtime's copies write their destination from their source alone — the
#: byte count and the kind carry no data (round 3 of this change's review: crediting the count
#: `flux.size() * sizeof(double)` passed a discarded result).
Summary = dict[int, frozenset[int]]
_RUNTIME_COPIES: dict[str, Summary] = {
    "cudaMemcpy": {0: frozenset({1})},
    "cudaMemcpyAsync": {0: frozenset({1})},
}
# A call: an optionally qualified name (`detail::axpy`, `dep_model::dep__flux`), optional template
# arguments and an optional kernel launch configuration, then `(`. A member call (`u.data()`,
# `p->f()`) is not one of the callees the closure follows.
_CALL_RE = re.compile(
    r"(?<![\w.])(?<!->)(?:[A-Za-z_]\w*\s*::\s*)*(?P<name>[A-Za-z_]\w*)\s*"
    r"(?:<[^<>;(){}]*>\s*)?(?:<<<.*?>>>\s*)?\(", re.DOTALL)
_VIEW_BRACE_RE = re.compile(r"^(?:spec2science\s*::\s*)?View\s*<[^{}]*>\s*\{(?P<body>.*)\}$", re.DOTALL)


def _payload(arg: str) -> str:
    """The part of an actual that carries data: the first element of a view built in place
    (`View<double, 1>{fp, {n}}` carries `fp`; its extents carry none — round 3 of this change's
    review: the extent `n` was read as a candidate and reached the output through an index);
    the actual itself otherwise."""
    arg = arg.strip()
    view = _VIEW_BRACE_RE.match(arg)
    if view is None:
        return arg
    return cpp_decls.split_top_level(view.group("body"))[0].strip()


def _call_records(body: str, summaries: dict[str, Summary]) -> list[
        tuple[str, set[str], int, str, bool]]:
    """One data edge per output actual of each call to a callee whose SUMMARY is known — a
    function or kernel this file defines (summarized from its body, `_function_summaries`), a
    dependency operation (its header: every input may reach every output), a CUDA runtime copy:
    the names the actual hands over take the identifiers of the input actuals its summary
    names. C++ states each parameter's direction in its type (`is_output_parameter`), which is
    what lets the C++ binding cross a call where the Fortran binding's gate does not. A call to
    anything else — a standard-library function, a function of another file — is not followed.
    Flagged as not an assignment statement, so a call's output is never "assigned before" a later
    call."""
    records: list[tuple[str, set[str], int, str, bool]] = []
    for call in _CALL_RE.finditer(body):
        summary = summaries.get(call.group("name"))
        if summary is None:
            continue
        args = _call_arguments(body, call.end() - 1)
        for out_index, in_indices in summary.items():
            if out_index >= len(args):
                continue
            inputs: set[str] = set()
            for index in in_indices:
                if index < len(args):
                    inputs |= _identifiers(_payload(args[index]))
            for name in _actual_names(args[out_index]):
                records.append((name, inputs, call.start(), "", True))
    return records


def _closure(seeds: set[str], records: list[tuple[str, set[str], int, str, bool]]) -> set[str]:
    """Every name some seed's value is computed from, backward over `records`."""
    sources = set(seeds)
    changed = True
    while changed:
        changed = False
        for lhs, rhs_ids, _pos, _rhs, _declared in records:
            if lhs in sources and not rhs_ids <= sources:
                sources |= rhs_ids
                changed = True
    return sources


#: What a function's RETURNED value is computed from: the positions of the parameters whose data
#: reaches its `return` (issue #380, round 2 of PR-1's review).
Returns = dict[str, frozenset[int]]


def _value_records(body: str, summaries: dict[str, Summary], returns: Returns) -> list[
        tuple[str, set[str], int, str, bool]]:
    """`_assignments` plus `_call_records` over `body`, with an assignment whose right-hand side
    CALLS a summarized function taking from that call only what the call returns: the actuals at
    the positions `returns` names (a function this file defines), or at the callee's input
    positions (one summarized from its declaration). `bad[i] = face_flux(u, fx, g, i)` takes the
    guard `face_flux` returns, not the `fx` it fills — which, read as a source, made a kernel's
    flag stand for the flux it dropped. The call's own name is no source either."""
    records: list[tuple[str, set[str], int, str, bool]] = []
    for lhs, ids, pos, rhs, declared in _assignments(body):
        if rhs:
            ids = _value_identifiers(rhs, summaries, returns)
        records.append((lhs, ids, pos, rhs, declared))
    return records + _call_records(body, summaries)


def _value_identifiers(expr: str, summaries: dict[str, Summary], returns: Returns) -> set[str]:
    """The identifiers `expr`'s VALUE is computed from: its own, with each summarized call
    contributing only the arguments its value is taken from — recursively, so a call nested in a
    taken argument (`to_flag(face(u, fo, g, k))`) is read the same way (round 3 of #380 PR-1's
    review)."""
    blanked = expr
    kept: set[str] = set()
    for call in _CALL_RE.finditer(expr):
        if call.start() < len(expr) and blanked[call.start()] == " ":
            # Inside a call already read (its taken arguments were recursed into). Not pinned:
            # without it a nested call's taken arguments are counted again at the top level,
            # which only adds input names (round 3's mutation sweep).
            continue
        name = call.group("name")
        if name not in summaries and name not in returns:
            continue
        open_at = call.end() - 1
        close_at = _close_paren(expr, open_at)
        args = _call_arguments(expr, open_at)
        if name in returns:
            take = returns[name]
        else:
            take = frozenset(i for i in range(len(args)) if i not in summaries[name])
        for i in take:
            if i < len(args):
                kept |= _value_identifiers(args[i], summaries, returns)
        blanked = blanked[:call.start()] + " " * (close_at + 1 - call.start()) + \
            blanked[close_at + 1:]
    return _identifiers(blanked) | kept


def _function_summaries(functions: list[cpp_decls.Function],
                        fixed: dict[str, Summary]) -> tuple[dict[str, Summary], Returns]:
    """`fixed` plus a summary of every function `functions` defines, computed from its BODY:
    which input parameters' data reaches each output parameter, through assignments, aliases and
    the calls it makes (round 3 of this change's review: a kernel credited from its signature
    passed although its body ignored the dependency's result); and, for a function returning a
    value, which parameters reach its `return` (`Returns`, read by `_value_records`). Computed
    to a fixed point from empty summaries, so a function is credited only with what its body
    shows, and a recursion terminates. Keyed by NAME and only ever widened, so two functions of
    one name (overloads, two namespaces) share the union and the iteration ends."""
    summaries: dict[str, Summary] = dict(fixed)
    for fn in functions:
        entry = dict(summaries.get(fn.name, {})) if fn.name not in fixed else {}
        for i, (ptype, _n) in enumerate(fn.params):
            if is_output_parameter(ptype):
                entry.setdefault(i, frozenset())
        summaries[fn.name] = entry
    returns: Returns = {fn.name: frozenset() for fn in functions if fn.returns != "void"}
    changed = True
    while changed:
        changed = False
        for fn in functions:
            records = _value_records(fn.body, summaries, returns)
            names = [name for _ptype, name in fn.params]
            current = summaries[fn.name]
            updated: Summary = dict(current)
            for out_index in current:
                if out_index >= len(names) or not names[out_index]:
                    continue
                reached = _closure({names[out_index]}, records)
                # Any OTHER parameter whose data reaches this output, whatever its type: a
                # kernel's non-const `double* f` that it only reads is an input of that call
                # (round 5 of this change's review: filtering by type dropped it).
                updated[out_index] = current[out_index] | frozenset(
                    i for i, name in enumerate(names)
                    if i != out_index and name and name in reached)
            if updated != current:
                summaries[fn.name] = updated
                changed = True
            if fn.name in returns:
                _outs, returned = _outputs(fn)
                reached = _closure(returned, records)
                now = returns[fn.name] | frozenset(
                    i for i, name in enumerate(names) if name and name in reached)
                if now != returns[fn.name]:
                    returns[fn.name] = now
                    changed = True
    return summaries, returns


def _call_arguments(body: str, open_at: int) -> list[str]:
    """The top-level arguments of the call whose `(` is at `open_at` in masked `body`."""
    close_at = _close_paren(body, open_at)
    inside = body[open_at + 1:close_at]
    return [arg.strip() for arg in cpp_decls.split_top_level(inside)] if inside.strip() else []


_PLAIN_NAME_RE = re.compile(r"(?<![\w.>:])([A-Za-z_]\w*)\b(?!\s*(?:::|<|\())")


def _actual_names(arg: str) -> set[str]:
    """The names whose storage an actual argument hands the callee: the bare name or `&name`;
    else every name whose storage the expression indexes or views (`u[i]`, `u.data()`, a view
    built over `u.data()`); else — the storage is reached some other way (`View<...>{p, ...}`
    over a pointer, `as_view(u)`, `w.flux.data()`) — every plain name the expression mentions,
    a type, a namespace and a called function excluded. The last clause exists because an actual
    whose storage name could not be read made the call's candidates EMPTY, which passed the gate
    with the result discarded (round 2 of this change's review)."""
    arg = _payload(arg)
    bare = re.fullmatch(r"&?\s*([A-Za-z_]\w*)", arg)
    if bare is not None:
        return {bare.group(1)}
    storage = _storage_names(arg)
    if storage:
        return storage
    return {name for name in _PLAIN_NAME_RE.findall(arg)
            if name not in cpp_signatures.CPP_KEYWORDS and name not in cpp_decls._TYPE_WORDS}


def _dependency_out_positions(model_file: Path, spec_id: str) -> dict[str, list[bool]] | None:
    """`{operation: [is output, per parameter]}` of dependency `spec_id`, read from its
    host-rendered header beside the model source, or None when the header is not there."""
    try:
        text = (model_file.parent / cpp_header.basename(spec_id)).read_text(
            encoding="utf-8", errors="ignore")
    except OSError:
        return None
    return {fn.name: [is_output_parameter(ptype) for ptype, _n in fn.params]
            for fn in cpp_decls.read(text).functions if fn.name.startswith(f"{spec_id}__")}


def _dependency_array_outputs(model_file: Path, spec_id: str) -> dict[str, set[int]]:
    """`{operation: positions of its ARRAY-typed output parameters}` of dependency `spec_id` (a
    non-const `spec2science::View`, `spec2science::Array&` / `std::vector&`, a pointer), from its header;
    empty when the header is not there."""
    try:
        text = (model_file.parent / cpp_header.basename(spec_id)).read_text(
            encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    return {fn.name: {i for i, (ptype, _n) in enumerate(fn.params)
                      if is_output_parameter(ptype) and _ARRAY_TYPE_RE.search(ptype.rstrip("&"))}
            for fn in cpp_decls.read(text).functions if fn.name.startswith(f"{spec_id}__")}


_INTEGER_TYPE_RE = re.compile(
    r"(?:const\s*)?(?:(?:unsigned|signed|short|long|int|(?:std::)?size_t|(?:std::)?ptrdiff_t"
    r"|(?:std::)?u?int(?:8|16|32|64)_t)\s*)+")
_INTEGER_DECLARATION_RE = re.compile(
    r"\b(?:unsigned|signed|short|long|int|(?:std::)?size_t|(?:std::)?ptrdiff_t"
    r"|(?:std::)?u?int(?:8|16|32|64)_t)\s+(?P<name>[A-Za-z_]\w*)\s*(?=[=;,{(])")


def _integer_names(fn: cpp_decls.Function) -> set[str]:
    """The names `fn` declares as integers — its by-value integer parameters and the integer
    locals of its body (`const long i = ...`, `for (int k = 0; ...)`): an index into a buffer,
    never the buffer. A pointer (`int* bad`) is not one."""
    names = {name for ptype, name in fn.params
             if name and _INTEGER_TYPE_RE.fullmatch(ptype.strip())}
    return names | {m.group("name") for m in _INTEGER_DECLARATION_RE.finditer(fn.body)}


# Issue #380 (round 2 of PR-1's review): what a dependency call WRITES is read as the STORAGE
# names its actual mentions — a name the function declares as a pointer, an array, a view or an
# owning container — not every plain name: `View<double, 1>{F + kComp * k, {3}}` hands over `F`,
# whatever the stride is spelled with (a file constant, a member, an `auto` index). Only when an
# actual mentions no declared storage does the reading fall back to its plain names, integers
# and file constants aside.
_STATEMENT_START = r"(?:^|(?<=[;{}]))\s*"
_STORAGE_QUALIFIERS = r"(?:(?:const|static|volatile|extern|__shared__|__constant__)\s+)*"
_POINTER_DECLARATION_RE = re.compile(
    _STATEMENT_START + _STORAGE_QUALIFIERS
    + r"(?P<type>[A-Za-z_][\w:]*(?:\s*<[^;{}()]*>)?)\s*\*+\s*"
    r"(?:(?:const|__restrict__|__restrict)\b\s*)*(?P<name>[A-Za-z_]\w*)\s*(?=[=;,\[{])",
    re.MULTILINE)
_ARRAY_DECLARATION_RE = re.compile(
    _STATEMENT_START + _STORAGE_QUALIFIERS
    + r"(?P<type>[A-Za-z_][\w:]*)\s+(?P<name>[A-Za-z_]\w*)\s*\[", re.MULTILINE)
_CONTAINER_DECLARATION_RE = re.compile(
    r"\b(?:std\s*::\s*vector|(?:spec2science\s*::\s*)?(?:View|Array))\s*<[^;{}]*?>\s*&?\s*"
    r"(?P<name>[A-Za-z_]\w*)\s*(?=[=;,({])")
_NOT_A_TYPE = frozenset({"return", "else", "case", "delete", "throw", "goto", "do", "new"})


def _storage_names_of(fn: cpp_decls.Function) -> set[str]:
    """The names `fn` declares as STORAGE: a parameter of an array type (a pointer, a view, an
    owning array or vector), and a local pointer, array, view or container."""
    names = {name for ptype, name in fn.params if name and _ARRAY_TYPE_RE.search(
        re.sub(r"\b__restrict(?:__)?\b|\bconst\s*$", "", ptype).strip().rstrip("&").strip())}
    for regex in (_POINTER_DECLARATION_RE, _ARRAY_DECLARATION_RE):
        names |= {m.group("name") for m in regex.finditer(fn.body)
                  if m.group("type").split("<")[0].strip() not in _NOT_A_TYPE}
    names |= {m.group("name") for m in _CONTAINER_DECLARATION_RE.finditer(fn.body)}
    return names


def _handed_over(arg: str, storage: set[str], fallback_excluded: set[str]) -> set[str]:
    """The names an actual hands a callee for writing: its declared storage names when it
    mentions any, else `_actual_names` minus `fallback_excluded` (integers, file constants)."""
    names = _actual_names(arg)
    held = names & storage
    return held if held else names - fallback_excluded


def _pointer_alias_records(fn: cpp_decls.Function, storage: set[str]) -> list[
        tuple[str, set[str], int, str, bool]]:
    """`p = <storage> + <offset>` (a declaration or a whole assignment of a pointer local) makes
    `p` point into that storage, so the storage takes `p` as a source, as `_assignments` does for
    `p = u.data()`: `double* fq = F + 3 * q;` then a write through `fq` is a write to `F`."""
    pointers = {m.group("name") for m in _POINTER_DECLARATION_RE.finditer(fn.body)}
    records: list[tuple[str, set[str], int, str, bool]] = []
    for m in _assignment_matches(fn.body):
        if m.lhs not in pointers or m.index.strip() or m.op != "=":
            continue
        for base in (_identifiers(m.rhs) & storage) - {m.lhs}:
            records.append((base, {m.lhs}, m.start, "", True))
    return records


def _signature_summary(out_at: list[bool]) -> Summary:
    """A callee known by its declaration only: every input may reach every output."""
    inputs = frozenset(i for i, out in enumerate(out_at) if not out)
    return {i: inputs for i, out in enumerate(out_at) if out}


def _outputs(fn: cpp_decls.Function) -> tuple[set[str], set[str]]:
    """`(output parameter names, the identifiers of every returned expression)`."""
    outs = {name for ptype, name in fn.params if name and is_output_parameter(ptype)}
    returned: set[str] = set()
    if fn.returns != "void":
        for m in _RETURN_RE.finditer(fn.body):
            returned |= _identifiers(m.group("expr"))
    return outs, returned


def _validate_problem_literal_outputs(model_file: Path, functions: list[cpp_decls.Function],
                                      violations: list[str]) -> None:
    """A function every one of whose output parameters is assigned only literals, none of them
    depending on an input, fabricates its outputs. Only a WHOLE assignment of an output
    (`out = 1.0;`) is read, as the Fortran gate reads `out = ...`; an output written element by
    element does not take part, and a function with such an output is not judged. The returned
    value is neither judged nor an exemption (the Fortran gate's reasoning: a constant accessor is
    the normal shape of a function, and a result must not launder a fabricated output)."""
    for fn in functions:
        outs = {name for ptype, name in fn.params if name and is_output_parameter(ptype)}
        if not outs:
            continue
        whole = [(m.lhs, m.rhs, m.op) for m in _assignment_matches(fn.body)
                 if m.lhs in outs and not m.index.strip()]
        if {lhs for lhs, _rhs, _op in whole} != outs:
            continue
        all_literal = all(_is_literal_like(rhs) for _lhs, rhs, _op in whole)
        # A compound assignment (`x += 1.0;`) reads the output's previous value — an input to the
        # function, since an output parameter is a reference the caller holds (Codex, round 2 of
        # this change's review: `x += 1.0` was refused as literal-only).
        # (The Fortran gate also exempts an output whose expression names an input; a literal-like
        # right-hand side names nothing, so that clause could never fire here, and round 4 of
        # this change's review found it unwitnessable. It is not kept.)
        input_dependent = any(op != "=" for _lhs, _rhs, op in whole)
        if all_literal and not input_dependent:
            violations.append(
                f"{model_file}: function {fn.name} has literal-only assignments for all output "
                "parameters")


def _validate_problem_dependency_dataflow(
    model_file: Path, functions: list[cpp_decls.Function], code: str, dep_spec_ids: list[str],
    violations: list[str],
) -> None:
    """Every dependency call of a function must let what that call WRITES flow to one of the
    function's outputs (the inert-call / discarded-result defect) — PER CALL, where the Fortran
    binding pools the candidates of every call of the function (round 3 of this change's review:
    one consumed result passed a second, discarded one, against the authoring rules).

    The candidate outputs of a dependency call are the names whose storage its actuals hand over,
    minus two kinds the Fortran binding's rule also drops: a parameter of the enclosing function
    (an output parameter already IS an output, and an input handed over for writing is the
    callee's business) and a name assigned before the call by an assignment STATEMENT — not a
    declaration's initializer, which the Fortran binding's pattern does not match either (an
    input the call reads — which is also how an INERT call keeps this gate silent, as the
    authoring rules tell the leaf). Which
    actuals are read at all is decided by the dependency's host-rendered header when it is beside
    the model source: only those at the operation's OUTPUT parameters — the Fortran binding's
    `intent(out)` question, answered by the declaration. Without the header every actual is read.
    Either way, an actual that names no declared storage loses the Fortran binding's two further
    kinds — a name the file declares `const` / `constexpr` (not definable) and the name of a
    function this file defines (a procedure argument) — and its integer names, unless that would
    leave the call no name at all (`dependency_calls`' `handed`).

    A function returning a value always takes part: its result is an output even when no
    identifier reaches its `return`, as the Fortran binding's result variable always is. The
    closure then runs backward from the function's outputs (and every identifier it returns)
    over assignments `lhs = rhs` — each `rhs` identifier is a source of `lhs` — over view and
    pointer aliases (`_assignments`), and over calls whose callee is SUMMARIZED
    (`_call_records`: this file's functions and kernels, from their bodies to a fixed point, not
    their signatures; the dependency operations, from their declarations; the CUDA runtime
    copies, from their source argument alone; a template is not read, so not followed). That last is where the C++ binding goes past the Fortran one, which follows
    no call because it cannot tell which argument a call writes; C++ states it in the type, and a
    device lowering (copy to the device, a kernel, copy back) has no assignment for the gate to
    follow otherwise (round 2 of this change's review). A call to anything else is not followed;
    the semantic authority is `Generate.verify`. An actual whose storage the call writes is read
    by `_handed_over`: the storage names the function declares that the actual mentions, else
    `_actual_names` (which falls back to every plain name the actual mentions) minus integers,
    file constants and this file's function names — and when that leaves the CALL nothing, its
    actuals' names unfiltered — so an output reached through a pointer, a helper or a member still yields a
    candidate.

    Every body read here has had its dead named lambdas blanked by `run_problem_model_gates`
    (`_blank_dead_lambdas`, issue #506): an assignment in a lambda the function never names again
    feeds no closure. A dead branch (`if (false) { … }`) and code that names the dead lambda
    (`(void)dead;`) still do; the gate does not read control flow, the same bound as the Fortran binding's residue
    (d). A dependency CALL in a dead lambda is absent too; the dependency-use check
    (`validate_dependency_operations`) reads the same blanked bodies, so a function whose only
    call never runs is refused there as calling none."""
    if not dep_spec_ids:
        return
    positions: dict[str, list[bool]] = {}
    array_outputs: dict[str, set[int]] = {}
    for spec_id in dep_spec_ids:
        array_outputs.update(_dependency_array_outputs(model_file, spec_id))
        read = _dependency_out_positions(model_file, spec_id)
        if read is not None:
            positions.update(read)
    constants = {m.group("name") for m in _CONST_DECLARATION_RE.finditer(code)}
    function_names = {fn.name for fn in functions}
    summaries, returns = _function_summaries(
        functions, {**{name: _signature_summary(out_at) for name, out_at in positions.items()},
                    **_RUNTIME_COPIES})
    call_re = re.compile(
        r"\b(?P<name>(?:" + "|".join(re.escape(s) for s in dep_spec_ids) + r")__\w+)\s*\(")
    # A function or kernel of this file whose OUTPUT PARAMETER receives what a dependency call
    # in its body writes stands for that dependency call in its callers (issue #380: a kernel may
    # call a `__host__ __device__` operation one element at a time, and the in-kernel call only
    # ever reaches the kernel's own output parameter, which the per-function check below
    # satisfies as already an output). `reached[f]` is the positions of `f`'s output parameters
    # the dependency's result reaches through `f`'s body — not every output of `f`: a kernel's
    # guard flag `int* bad` passed on to `ok` must not stand for the flux it discards (round 1 of
    # #380 PR-1's review), nor a flag taken from what a helper RETURNS while it fills the flux
    # (`_value_records`, round 2). Computed to a fixed point, so a host helper that
    # launches such a kernel stands for it too; keyed by NAME, as `_function_summaries` is, and
    # only ever widened, so two functions of one name (overloads, two namespaces) share the union
    # and the iteration ends (round 2: an alternating pair never converged).
    by_name = {fn.name: fn for fn in functions}
    storage_of = {id(fn): _storage_names_of(fn) for fn in functions}
    excluded_of = {id(fn): _integer_names(fn) | constants | function_names for fn in functions}
    records_of = {id(fn): (_value_records(fn.body, summaries, returns)
                           + _pointer_alias_records(fn, storage_of[id(fn)]))
                  for fn in functions}

    def dependency_calls(fn: cpp_decls.Function, reached: dict[str, frozenset[int]]) -> list[
            tuple[int, int, list[bool] | None, set[int]]]:
        """`(start, open paren, output positions or None, array output positions)` of every
        dependency call in `fn`'s body — an operation of a dependency, or a function of this
        file that `reached` says carries one's result."""
        # An operation that writes an ARRAY must have an array result reach the output: its
        # scalar outputs (a guard flag) do not stand for it (round 5 of this change's review:
        # `ok = guard_pass;` alone made every call of the advdiff problem "propagated" while its
        # fluxes were discarded).
        calls = [(call.start(), call.end() - 1, positions.get(call.group("name")),
                  array_outputs.get(call.group("name"), set()))
                 for call in call_re.finditer(fn.body)]
        for call in _CALL_RE.finditer(fn.body):
            callee = by_name.get(call.group("name"))
            if callee is None or callee.name == fn.name or not reached.get(callee.name):
                continue
            # No array narrowing here: `reached` already holds only the positions the
            # dependency's (array, when it has one) result reaches.
            hit = reached[callee.name]
            calls.append((call.start(), call.end() - 1,
                          [i in hit for i in range(len(callee.params))], set()))
        return calls

    def handed(fn: cpp_decls.Function, open_at: int, out_at: list[bool] | None,
               arrays: set[int]) -> list[set[str]]:
        """Per written actual of the call whose `(` is at `open_at`, the names it hands over."""
        written: list[str] = []
        for index, arg in enumerate(_call_arguments(fn.body, open_at)):
            if out_at is not None and not (index < len(out_at) and out_at[index]):
                continue
            if arrays and index not in arrays:
                continue
            written.append(arg)
        out = [_handed_over(arg, storage_of[id(fn)], excluded_of[id(fn)]) for arg in written]
        # The exclusions never leave a CALL without a name: one with no candidate passes the
        # per-call check unasked (round 3 of #380 PR-1's review — a buffer from `buf_of(k)`, a
        # bare `int&` result, a namespace-scope `double* const` each lost its only name). A
        # single actual may still lose its own (a `const` or a function handed over among
        # others, as in the Fortran binding).
        if not any(out):
            out = [_actual_names(arg) for arg in written]
        return out

    reached: dict[str, frozenset[int]] = {}
    changed = True
    while changed:
        changed = False
        for fn in functions:
            hands_over: set[str] = set()
            for _start, open_at, out_at, arrays in dependency_calls(fn, reached):
                for names in handed(fn, open_at, out_at, arrays):
                    hands_over |= names
            now = frozenset(
                i for i, (ptype, name) in enumerate(fn.params)
                if hands_over and name and is_output_parameter(ptype)
                and _closure({name}, records_of[id(fn)]) & hands_over)
            widened = now | reached.get(fn.name, frozenset())
            if widened != reached.get(fn.name, frozenset()):
                reached[fn.name] = widened
                changed = True
    for fn in functions:
        params = {name for _ptype, name in fn.params if name}
        out_params = {name for ptype, name in fn.params if name and is_output_parameter(ptype)}
        outs, returned = _outputs(fn)
        # A function returning a value always has an output — its result — even when no
        # identifier reaches its `return` (`return 1.0;`): the Fortran binding's result variable
        # is always an output. Skipped only when nothing leaves the function.
        if not outs and fn.returns == "void":
            continue
        records = records_of[id(fn)]
        sources = _closure(set(outs) | returned, records)
        discarded: set[str] = set()
        for call_start, open_at, out_at, arrays in dependency_calls(fn, reached):
            per_actual = handed(fn, open_at, out_at, arrays)
            # A call that writes one of the function's OUTPUT parameters has its result at an
            # output already (round 2 of #380 PR-1's review: a kernel writing `F_star` straight
            # into its output `F` and `G_star` into a local it does not need was refused on the
            # local alone).
            if any(names & out_params for names in per_actual):
                continue
            candidates: set[str] = set()
            for names in per_actual:
                for name in names:
                    # The enclosing function's parameters drop out (an input handed over for
                    # writing is the callee's business), and so does a name assigned before the
                    # call by an assignment STATEMENT (an input the call reads).
                    if name in params or any(lhs == name and pos < call_start
                                             and not declared
                                             for lhs, _ids, pos, _rhs, declared in records):
                        continue
                    candidates.add(name)
            # PER CALL: what THIS call writes must reach an output. The Fortran binding pools the
            # candidates of every call of the function, so one call's result reaching the output
            # passes another call whose result is discarded — which contradicts what the
            # authoring rules tell the leaf (round 3 of this change's review).
            if candidates and candidates.isdisjoint(sources):
                discarded |= candidates
        if discarded:
            violations.append(
                f"{model_file}: function {fn.name} does not propagate dependency operation "
                f"outputs to its output dataflow (candidates={sorted(discarded)})")


def _validate_problem_metric_only_scalar_kernel(
    multidim_spec_id: str | None, model_file: Path, functions: list[cpp_decls.Function],
    violations: list[str],
) -> None:
    """On a multi-dimensional `problem` node, a function with five or more outputs (output
    parameters, plus the returned value) and neither an array input nor a loop derives metrics
    without a kernel. An array input is a parameter of an array type (`spec2science::View`,
    `spec2science::Array`, `std::vector`, a pointer); a loop is a `for`, a `while` or a kernel launch
    (`<<<`)."""
    if multidim_spec_id is None:
        return
    for fn in functions:
        outs = [name for ptype, name in fn.params if name and is_output_parameter(ptype)]
        count = len(outs) + (0 if fn.returns == "void" else 1)
        if count < 5:
            continue
        has_array_input = any(_ARRAY_TYPE_RE.search(ptype) for ptype, _n in fn.params)
        if has_array_input or _LOOP_RE.search(fn.body):
            continue
        violations.append(
            f"{model_file}: function {fn.name} is metric-only scalar kernel for "
            f"{multidim_spec_id}; 2d/3d problem model must not derive many outputs without "
            "array inputs or update loops")


def validate_member_init_placement(
    *,
    node_spec_id: str,
    model_file: Path,
    text: str,
    functions: list[cpp_decls.Function],
    init_member_ids: Iterable[str],
    violations: list[str],
) -> None:
    """The C++ reading of the Fortran binding's `validate_member_init_placement` (the system
    tier, Part 2; `docs/CONTROLLED_SPEC.md`), over ``functions`` (the source's DEFINED functions,
    dead named lambdas blanked) and the masked source with its preprocessor lines kept:

    - PRESENCE: the body of `<node>__init` calls `<member>__init(...)` (qualified or not);
    - ABSENCE: every token `<member>__init` of the masked source is such a call inside
      `<node>__init`'s body, so a pointer or reference alias (`auto f = &<member>__init;`), an
      actual argument, a declaration and a call from another function are each refused. The C++
      binding has no import rename, and a macro that respells the name is refused by the
      preprocessor allowlist (`preprocessor_violations`) before this reads anything;
    - the composite's own `<node>__init` token occurs only as the name of a function declaration
      or definition: a call of it from `__step` reaches the member's `__init` from there.

    Every OTHER `.cu` of the source directory (`leaf_sources`, the host-rendered runner excepted)
    names neither `<member>__init` nor `<spec_id>__init`: a helper that calls the member's `__init`
    is otherwise reached from `__step` through a function whose name the model's count never
    sees (review round 1 of issue #632 P3).

    Absence counts TOKENS, presence reads calls, for the polarity reason the Fortran binding
    states. What a token cannot show, and the continuation case judges: a lambda written inside
    `<node>__init` whose body calls the member's `__init` and that is stored beyond the call
    (`g = [&]{ <member>__init(...); };`). A leaf gains no verdict from it: the plain call is the
    cheaper edit."""
    code = cpp_lines.mask(text)
    node_init = f"{node_spec_id}__init"
    inits = [fn for fn in functions if fn.name == node_init]
    declared = sum(1 for fn in cpp_decls.read(text).functions if fn.name == node_init)
    own = len(re.findall(rf"(?<![\w]){re.escape(node_init)}(?!\w)", code))
    if own != declared:
        violations.append(
            f"{model_file}: `{node_init}` is named outside its own declaration ({own} "
            f"occurrence(s), {declared} declaration(s)): the model never calls or references its "
            f"own `__init` — the driver calls it once per case, and a call from `__step` or any "
            f"other function reaches the member's `__init` from there.")
    host_runner = model_file.with_name(model_file.name.removesuffix("_model.cu") + "_runner.cu")
    for source in leaf_sources(model_file.parent):
        if source in (model_file, host_runner):
            continue
        other = cpp_lines.mask(source.read_text(encoding="utf-8", errors="ignore"))
        for name in sorted({node_init} | {f"{m}__init" for m in init_member_ids}):
            if re.search(rf"(?<![\w]){re.escape(name)}(?!\w)", other):
                violations.append(
                    f"{source}: `{name}` is named in a source other than {model_file.name}. A "
                    f"member's `__init` is called from `{node_init}` in the model file and named "
                    f"nowhere else, and no other source names `{node_init}`: keep the call in "
                    f"`{node_init}` and add no private file that names either.")
    for member in sorted(set(init_member_ids)):
        member_init = f"{member}__init"
        token = re.compile(rf"(?<![\w]){re.escape(member_init)}(?!\w)")
        callee = re.compile(rf"(?<![\w]){re.escape(member_init)}\s*\(")
        if not inits:
            violations.append(
                f"{model_file}: the member `{member}` publishes `{member_init}`, and this model "
                f"defines no `{node_init}`: a composite whose member publishes `__init` defines "
                f"`{node_init}` (its §5.1 publishes it) and calls `{member_init}(...)` there.")
            continue
        calls_in_init = sum(len(callee.findall(fn.body)) for fn in inits)
        if not calls_in_init:
            violations.append(
                f"{model_file}: `{node_init}` does not call `{member_init}(...)`: the "
                f"composite's `__init` calls the member's `__init` by that name in its own body, "
                f"live, once per case.")
        total = len(token.findall(code))
        if total != calls_in_init:
            violations.append(
                f"{model_file}: `{member_init}` is named {total - calls_in_init} time(s) other "
                f"than as a call inside `{node_init}`. The member's `__init` is called from "
                f"`{node_init}` and named nowhere else: not from `__step` or another function, "
                f"not through a pointer, a reference or a lambda, not as an argument, not in a "
                f"declaration. Both halves hold together — the call inside `{node_init}`, and no "
                f"other occurrence of the name.")


def run_problem_model_gates(
    node_key: str,
    model_file: Path,
    text: str,
    dep_spec_ids: list[str],
    violations: list[str],
    *,
    multidim_spec_id: str | None,
    init_member_ids: Iterable[str],
) -> None:
    """The three `problem` model gates over one model source, read once, and the member
    `__init` placement gate (`validate_member_init_placement`) when ``init_member_ids`` names a
    member. Scoped to `problem/`, as the Fortran binding's are. A source whose declarations do not read is REFUSED here — every
    gate would otherwise pass what it could not see. Each function body is read with its dead
    named lambdas blanked (`_blank_dead_lambdas`, issue #506)."""
    if not node_key.startswith("problem/"):
        return
    decls = cpp_decls.read(text)
    if decls.errors:
        violations.append(_structure_refusal(model_file, decls.errors,
                                             "the `problem` model gates"))
        return
    # A named lambda the body never uses again is blanked out of every function body before any
    # gate (or `_function_summaries`) reads it (issue #506); `code` below, the file-wide view the
    # constant set is read from, is not touched.
    functions = [dataclasses.replace(fn, body=_blank_dead_lambdas(fn.body))
                 for fn in decls.functions if fn.defined]
    code = cpp_lines.strip_preprocessor(cpp_lines.mask(text))
    _validate_problem_literal_outputs(model_file, functions, violations)
    _validate_problem_dependency_dataflow(model_file, functions, code, dep_spec_ids, violations)
    _validate_problem_metric_only_scalar_kernel(multidim_spec_id, model_file, functions,
                                                violations)
    init_member_ids = tuple(init_member_ids)
    if init_member_ids:
        validate_member_init_placement(
            node_spec_id=node_key.split("/", 1)[1].split("@", 1)[0].strip(),
            model_file=model_file, text=text, functions=functions,
            init_member_ids=init_member_ids, violations=violations)
