"""How the deterministic `Generate` gates read a CUDA C++ source (issue #289, R4-b PR-4).

The `source_reading` capability. Every reader here works on `lines.mask` of the text, so a comment
or the inside of a literal never answers a question about code.

WHAT IS IMPLEMENTED. Every gate a node of this language reaches: the build control file's source
facts (`MODULE_SOURCE_SUFFIXES`, `source_module_deps`), the runner scans
(`validate_runner_json_serialization`, `validate_runner_snapshot_filenames`), the model gates
(`model_source_gates`: the literal-metric floors, the preprocessor allowlist, and on a `problem`
node the three model gates of `run_problem_model_gates`), the loop count the parallel presence
floor reads (`counted_loops`), the absent-model remedy, and — since the host renders a physics
node's runner (issue #289, R4-b PR-6) — the checks-source gates (`checks_module_declaration_
violations`, `checks_module_abi_facts`, `unpublished_bound_state`,
`checks_harness_isolation_violations`) and the dependency-use gate
(`validate_dependency_operations`). The C++ binding each implements is stated for a reader in
`docs/backends/language/cuda_cpp/CHECKS_ABI.md` and `GENERATE_RULES.md`.

A source whose structure `declarations.read` cannot resolve (an unbalanced bracket) is REFUSED by
the gates that read declarations, never passed: a reader that stopped early would report nothing
found, and for a gate that subtracts candidates that is a silent pass.

Imports this package's readers, and through `signatures` the neutral structured-signature form;
nothing else from the neutral core.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from tools.backends.language.cuda_cpp import checks_abi
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
    """Every regular `.cu` file under `src_dir`, at any depth (a bundle file may sit in a
    subdirectory and be included from there), symbolic links not followed."""
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
) -> None:
    """The structural gates over one model source.

    Every node: the case-id-keyed metric floor (a `case_id` comparison beside a literal-indexed
    `metrics[...]`), the literal-metric floor (six or more `metrics[<n>] = <literal>`
    assignments), and the preprocessor allowlist (`preprocessor_violations`) — the last over EVERY
    `.cu` under the model's source directory at any depth (`leaf_sources`), because a directive in
    a file the model or runner includes reaches the gates through it — and the rule that every
    `.cu` sits at the top level of that directory. On a `problem` node, the three model gates
    (`run_problem_model_gates`)."""
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
    for source in leaf_sources(model_file.parent):
        source_text = (text if source == model_file
                       else source.read_text(encoding="utf-8", errors="ignore"))
        violations.extend(preprocessor_violations(source, source_text))
        if source.parent != model_file.parent:
            # Build compiles every `.cu` as its own object, and the syntax stage compiles only
            # the top level (the compiler tool takes no path below its directory): a nested one
            # would reach Build uncompiled for the target, or — included from a top-level file
            # as well — be compiled twice (round 3 of this change's review).
            violations.append(
                f"{source}: a CUDA C++ source in a subdirectory is refused — every `.cu` is a "
                "translation unit of its own and sits beside the host-rendered header at the top "
                "of the source directory (move it there, and do not `#include` a `.cu`)")
    run_problem_model_gates(node_key, model_file, text, dep_spec_ids, violations,
                            multidim_spec_id=multidim_spec_id)


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
    depth = 0
    for index in range(open_at, len(code)):
        if code[index] == "(":
            depth += 1
        elif code[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return len(code)


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
    directive in the runner is judged by `model_source_gates`, which reads every leaf source."""
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


# --- the checks-source gates (a physics node; CHECKS_ABI.md §1-§4) ------------------------------

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


def checks_module_declaration_violations(checks_path: Path, text: str, spec_id: str) -> list[str]:
    """The checks source includes the host-rendered checks header and opens namespace
    `<spec_id>_checks` — the two facts every other checks gate reads the source by."""
    out: list[str] = []
    header_name = checks_abi.checks_header_basename(spec_id)
    if header_name not in {name for _line, name in quoted_includes(text)}:
        out.append(
            f"{checks_path}: must `#include \"{header_name}\"` — the host-rendered header "
            "declares the checks ABI and the bound state this source DEFINES, and including it is "
            "what makes a definition of another type a compile error instead of a silent "
            "mismatch at link")
    decls = cpp_decls.read(text)
    if decls.errors:
        out.append(_structure_refusal(checks_path, decls.errors, "the checks-source gate"))
    elif (f"{spec_id}_checks",) not in decls.namespaces:
        out.append(
            f"{checks_path}: must define the checks ABI in `namespace {spec_id}_checks {{ ... }}` "
            "(the namespace the host-rendered runner calls into)")
    return out


#: Head words that give a namespace-scope function or variable something other than one external
#: definition the runner's translation unit can link against: internal linkage (`static`), a
#: definition every translation unit must repeat (`inline`, and `constexpr`, which implies it),
#: or a declaration rather than a definition (`extern`).
_NOT_EXTERNAL_DEFINITION = frozenset({"static", "inline", "__inline__", "__forceinline__",
                                      "constexpr", "extern", "const"})


def checks_module_abi_facts(text: str, spec_id: str) -> tuple[set[str], set[str], set[str]]:
    """`(published, defined_subroutines, defined_procs)` for namespace `<spec_id>_checks` of
    `text` — the facts the `Generate.gate` static check and the bundle acceptance gate share.

    `defined_procs` is every name DEFINED in that namespace (a qualified definition
    `void <spec_id>_checks::f(...) {...}` included). `defined_subroutines` is the subset that is an
    ABI callback defined as the header declares it: returning `void`, with exactly the declared
    parameter types in order (`checks_abi.CHECKS_ABI_PARAMS`, names free), and with one external
    definition (no `static` / `inline` / `constexpr`). `published` is `defined_subroutines`: in
    C++ nothing but that definition satisfies the runner's call — a definition with other
    parameter types is an OVERLOAD the header's declaration never reaches (a link error), a
    `__device__` or `static` one is not callable from the runner's translation unit — so a name
    defined any other way reads as unpublished, and the caller's remedy states the declaration.

    A source whose declarations do not read (`declarations.read(...).errors`) publishes nothing,
    so every caller refuses it; `checks_module_declaration_violations` names why."""
    decls = cpp_decls.read(text)
    if decls.errors:
        return set(), set(), set()
    namespace = (f"{spec_id}_checks",)
    defined: set[str] = set()
    subroutines: set[str] = set()
    for fn in decls.functions:
        if not (fn.defined and fn.namespace == namespace):
            continue
        defined.add(fn.name)
        declared = checks_abi.CHECKS_ABI_PARAMS.get(fn.name)
        if declared is None or fn.returns != "void":
            continue
        if set(fn.head.split(" ")) & _NOT_EXTERNAL_DEFINITION:
            continue
        if tuple(ptype for ptype, _name in fn.params) != tuple(ptype for ptype, _n in declared):
            continue
        subroutines.add(fn.name)
    return set(subroutines), subroutines, defined


def unpublished_bound_state(text: str, spec_id: str, bound: Iterable[str]) -> list[str]:
    """The `bound` names that `text` does not DEFINE at namespace scope of `<spec_id>_checks`
    with external linkage — the definition the host-rendered header's `extern` declaration and
    the runner's `<spec_id>_checks::<name>` reach.

    A definition is a namespace-scope variable of that namespace whose declaration carries none
    of `static` / `const` / `constexpr` / `extern` / `inline` (internal linkage, or a declaration
    only). A direct-initialized one (`std::vector<double> u(3);`) is read by `declarations` as a
    function declaration, so a non-defining function declaration of the name counts as its
    definition too; the header's `extern` declaration then makes any type disagreement a compile
    error, which is where the TYPE is judged. A source whose declarations do not read defines
    nothing (every bound name is reported)."""
    decls = cpp_decls.read(text)
    names = list(bound)
    if decls.errors:
        return names
    namespace = (f"{spec_id}_checks",)
    defined: set[str] = set()
    for var in decls.variables:
        if var.namespace == namespace and not (
                set(var.statement.split(" ")) & _NOT_EXTERNAL_DEFINITION):
            defined.add(var.name)
    for fn in decls.functions:
        if (fn.namespace == namespace and not fn.defined
                and not (set(fn.head.split(" ")) & _NOT_EXTERNAL_DEFINITION)):
            defined.add(fn.name)
    return [name for name in names if name not in defined]


_HARNESS_REFERENCE_RE = re.compile(r"\bharness_\w*_model\s*::|\bharness_\w+__\w+")

#: What opens, writes, renames or deletes a file, or runs a command, in a checks source — every
#: file-stream class (`ofstream`, `fstream`, `basic_ofstream<char>`, the wide ones), the C stdio
#: and POSIX openers, `std::filesystem`, `rename` / `remove`, and `system` / `popen`. Wider than
#: the runner scan's `_FILE_OPEN_RE` on purpose: round 1 of this change's review wrote the run's
#: outputs from a checks source through `std::fstream` and `std::system` with the narrower one
#: silent, and the Fortran binding's `open(` covers every way its language opens a file.
_CHECKS_IO_RE = re.compile(
    r"\b\w*fstream\b|\b(?:fopen|freopen|fdopen|popen|open|creat|rename|remove|system)\s*\("
    r"|\bfilesystem\b")


def checks_harness_isolation_violations(
    checks_path: Path, text: str, model_files: list[Path]
) -> list[str]:
    """The isolation half of the checks-source gate: neither physics source includes a harness
    header or names the harness (`harness_<x>_model::`, `harness_<x>__<op>`) — the host-rendered
    runner is the only caller of the harness — and the checks source opens no file (emission is
    the harness's). `text` is the checks source's raw content; each model source is read here."""
    violations: list[str] = []
    for path in [checks_path, *model_files]:
        source = text if path == checks_path else path.read_text(encoding="utf-8",
                                                                  errors="ignore")
        harness_includes = [name for _line, name in quoted_includes(source)
                            if Path(name).name.startswith("harness_")]
        code = cpp_lines.strip_preprocessor(cpp_lines.mask(splice_lines(source)))
        if harness_includes or _HARNESS_REFERENCE_RE.search(code):
            violations.append(
                f"{path}: a physics source must not include or name the harness — the physics "
                "node never depends on the harness at the source level (the host-rendered "
                "runner is the sole caller of the harness)")
    io = _CHECKS_IO_RE.search(cpp_lines.strip_preprocessor(cpp_lines.mask(splice_lines(text))))
    if io:
        violations.append(
            f"{checks_path}: the checks source must not do file I/O or run a command "
            f"(`{io.group(0).strip()}` — no file stream, `fopen`, `std::filesystem`, `rename` / "
            "`remove`, `system` / `popen`) — emission is the harness's job; the checks source "
            "only holds the state and computes the checks and metrics")
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
    operation from a function body (qualified `<dep>_model::` or not). The three clauses of the
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
        bodies = [fn.body for fn in decls.functions if fn.defined]
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
                    f"{model_file}: missing dependency operation call ({spec_id}__*)")


# --- the three `problem` model gates -----------------------------------------------------------
#
# The C++ reading of the Fortran binding's gates (`tools/backends/language/fortran/source.py`
# `run_problem_model_gates`), over `declarations.read`'s namespace-scope function definitions. A
# parameter is an OUTPUT when the function can write through it: a non-const reference, a pointer
# to non-const, or a non-const `atmofab::View`; everything else is an input. A function returning
# a value has that value as one more output (the Fortran function result), read off its `return`
# statements. An assignment is `<name> [index...] op= <expr>;` over the masked body, with the
# name the base of the target (`u[i] = ...`, `u.data[i] = ...`).

_OUT_VIEW_RE = re.compile(r"^(?:atmofab::)?View<(?P<element>.+),\d+>$")
_ARRAY_TYPE_RE = re.compile(r"\b(?:View|Array|vector)\s*<|\*$")
_ASSIGNMENT_RE = re.compile(
    r"(?<![\w.>:])(?P<lhs>[A-Za-z_]\w*)\s*"
    r"(?P<index>(?:\[[^\];]*\]\s*|\.\s*data\s*(?:\(\s*\))?\s*\[[^\];]*\]\s*)*)"
    r"(?P<op>[-+*/%&|^]?=)(?!=)(?P<rhs>[^;]*);")
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
    """Whether a function can write through a parameter of (normalized) type `ctype`."""
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


def _assignments(body: str) -> list[tuple[str, set[str], int, str, bool]]:
    """`(target name, right-hand identifiers, offset, right-hand text, is a declaration)` per
    assignment of `body`, plus one ALIAS record per view declared over another name's storage
    (`View<double, 1> v{u.data(), ...}`): a write through `v` is a write to `u`, so `u` takes `v`
    as a source. A declaration's initializer carries data like any assignment; it is flagged
    because the dataflow gate's "assigned before the call" clause reads assignment STATEMENTS
    only, as the Fortran binding's does (its assignment pattern does not match a declaration)."""
    records = [(m.group("lhs"), _identifiers(m.group("rhs")), m.start(), m.group("rhs").strip(),
                _is_declaration(body, m.start()))
               for m in _ASSIGNMENT_RE.finditer(body)]
    for m in _VIEW_DECLARATION_RE.finditer(body):
        for storage in _STORAGE_NAME_RE.findall(m.group("init")):
            records.append((storage, {m.group("name")}, m.start(), "", True))
    return records


def _call_arguments(body: str, open_at: int) -> list[str]:
    """The top-level arguments of the call whose `(` is at `open_at` in masked `body`."""
    close_at = _close_paren(body, open_at)
    inside = body[open_at + 1:close_at]
    return [arg.strip() for arg in cpp_decls.split_top_level(inside)] if inside.strip() else []


def _actual_names(arg: str) -> set[str]:
    """The names whose storage an actual argument hands the callee: the bare name, `&name`, and
    every name whose storage the expression indexes or views (`u[i]`, `u.data()`, a view built
    over `u.data()`). Any other expression hands over no storage and yields nothing."""
    arg = arg.strip()
    bare = re.fullmatch(r"&?\s*([A-Za-z_]\w*)", arg)
    if bare is not None:
        return {bare.group(1)}
    return set(_STORAGE_NAME_RE.findall(arg))


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
        inputs = {name for _ptype, name in fn.params if name}
        whole = [(m.group("lhs"), m.group("rhs")) for m in _ASSIGNMENT_RE.finditer(fn.body)
                 if m.group("lhs") in outs and not m.group("index").strip()]
        if {lhs for lhs, _rhs in whole} != outs:
            continue
        all_literal = all(_is_literal_like(rhs) for _lhs, rhs in whole)
        input_dependent = any(_identifiers(rhs) & (inputs - {lhs}) for lhs, rhs in whole)
        if all_literal and not input_dependent:
            violations.append(
                f"{model_file}: function {fn.name} has literal-only assignments for all output "
                "parameters")


def _validate_problem_dependency_dataflow(
    model_file: Path, functions: list[cpp_decls.Function], code: str, dep_spec_ids: list[str],
    violations: list[str],
) -> None:
    """A function that calls a dependency operation must let what that operation WRITES flow to
    one of its own outputs, through assignments (the inert-call / discarded-result defect).

    The candidate outputs of a dependency call are the names whose storage its actuals hand over,
    minus two kinds the Fortran binding's rule also drops: a parameter of the enclosing function
    (an output parameter already IS an output, and an input handed over for writing is the
    callee's business) and a name assigned before the call by an assignment STATEMENT — not a
    declaration's initializer, which the Fortran binding's pattern does not match either (an
    input the call reads — which is also how an INERT call keeps this gate silent, as the
    authoring rules tell the leaf). Which
    actuals are read at all is decided by the dependency's host-rendered header when it is beside
    the model source: only those at the operation's OUTPUT parameters — the Fortran binding's
    `intent(out)` question, answered by the declaration. Without the header every actual is read,
    minus the Fortran binding's two further kinds: a name the file declares `const` / `constexpr`
    (not definable) and the name of a function this file defines (a procedure argument).

    A function returning a value always takes part: its result is an output even when no
    identifier reaches its `return`, as the Fortran binding's result variable always is. The
    closure then runs backward from the function's outputs (and every identifier it returns)
    over assignments `lhs = rhs` — each `rhs` identifier is a source of `lhs` — and over view
    aliases (`_assignments`). Assignments only: whether a value passed to another call is read or
    written cannot be decided here, which is the Fortran gate's stated limit too; the semantic
    authority is `Generate.verify`."""
    if not dep_spec_ids:
        return
    positions: dict[str, list[bool]] = {}
    for spec_id in dep_spec_ids:
        read = _dependency_out_positions(model_file, spec_id)
        if read is not None:
            positions.update(read)
    constants = {m.group("name") for m in _CONST_DECLARATION_RE.finditer(code)}
    function_names = {fn.name for fn in functions}
    call_re = re.compile(
        r"\b(?P<name>(?:" + "|".join(re.escape(s) for s in dep_spec_ids) + r")__\w+)\s*\(")
    for fn in functions:
        params = {name for _ptype, name in fn.params if name}
        outs, returned = _outputs(fn)
        # A function returning a value always has an output — its result — even when no
        # identifier reaches its `return` (`return 1.0;`): the Fortran binding's result variable
        # is always an output. Skipped only when nothing leaves the function.
        if not outs and fn.returns == "void":
            continue
        records = _assignments(fn.body)
        candidates: set[str] = set()
        for call in call_re.finditer(fn.body):
            args = _call_arguments(fn.body, call.end() - 1)
            out_at = positions.get(call.group("name"))
            for index, arg in enumerate(args):
                if out_at is not None and not (index < len(out_at) and out_at[index]):
                    continue
                for name in _actual_names(arg):
                    if name in params or any(lhs == name and pos < call.start()
                                             and not declared
                                             for lhs, _ids, pos, _rhs, declared in records):
                        continue
                    if out_at is None and (name in constants or name in function_names):
                        continue
                    candidates.add(name)
        if not candidates:
            continue
        sources = set(outs) | returned
        changed = True
        while changed:
            changed = False
            for lhs, rhs_ids, _pos, _rhs, _declared in records:
                if lhs in sources and not rhs_ids <= sources:
                    sources |= rhs_ids
                    changed = True
        if candidates.isdisjoint(sources):
            violations.append(
                f"{model_file}: function {fn.name} does not propagate dependency operation "
                f"outputs to its output dataflow (candidates={sorted(candidates)})")


def _validate_problem_metric_only_scalar_kernel(
    multidim_spec_id: str | None, model_file: Path, functions: list[cpp_decls.Function],
    violations: list[str],
) -> None:
    """On a multi-dimensional `problem` node, a function with five or more outputs (output
    parameters, plus the returned value) and neither an array input nor a loop derives metrics
    without a kernel. An array input is a parameter of an array type (`atmofab::View`,
    `atmofab::Array`, `std::vector`, a pointer); a loop is a `for`, a `while` or a kernel launch
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


def run_problem_model_gates(
    node_key: str,
    model_file: Path,
    text: str,
    dep_spec_ids: list[str],
    violations: list[str],
    *,
    multidim_spec_id: str | None,
) -> None:
    """The three `problem` model gates over one model source, read once. Scoped to `problem/`,
    as the Fortran binding's are. A source whose declarations do not read is REFUSED here — every
    gate would otherwise pass what it could not see."""
    if not node_key.startswith("problem/"):
        return
    decls = cpp_decls.read(text)
    if decls.errors:
        violations.append(_structure_refusal(model_file, decls.errors,
                                             "the `problem` model gates"))
        return
    functions = [fn for fn in decls.functions if fn.defined]
    code = cpp_lines.strip_preprocessor(cpp_lines.mask(text))
    _validate_problem_literal_outputs(model_file, functions, violations)
    _validate_problem_dependency_dataflow(model_file, functions, code, dep_spec_ids, violations)
    _validate_problem_metric_only_scalar_kernel(multidim_spec_id, model_file, functions,
                                                violations)
