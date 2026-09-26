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
from typing import NamedTuple

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


_EAST_CONST_RE = re.compile(r"^(?P<base>[^&*]+?) const(?P<ref>[&*]?)$")


def _canonical_type(ctype: str) -> str:
    """A normalized parameter type with an east `const` moved west (`std::string const&` ->
    `const std::string&`): one type, two spellings, and the ABI table spells the west one."""
    east = _EAST_CONST_RE.match(ctype)
    return f"const {east.group('base')}{east.group('ref')}" if east else ctype


def _declarator_names(statement: str) -> list[str]:
    """Every name a namespace-scope variable declaration declares (`std::vector<double> a, b`
    declares both): the trailing identifier of each top-level declarator, its initializer — `=`,
    a brace or a parenthesis group — removed."""
    names: list[str] = []
    for declarator in cpp_decls.split_top_level(statement):
        head = cpp_decls.split_top_level(declarator, "=")[0]
        head = re.sub(r"[({][^(){}]*[)}]\s*$", "", head.strip())
        m = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*$", head)
        if m is not None:
            names.append(m.group(1))
    return names


def checks_module_abi_facts(text: str, spec_id: str) -> tuple[set[str], set[str], set[str]]:
    """`(published, defined_subroutines, defined_procs)` for namespace `<spec_id>_checks` of
    `text` — the facts the `Generate.gate` static check and the bundle acceptance gate share.

    `defined_procs` is every name DEFINED in that namespace (a qualified definition
    `void <spec_id>_checks::f(...) {...}` included). `defined_subroutines` is the subset that is an
    ABI callback defined as the header declares it: returning `void`, with exactly the declared
    parameter types in order (`checks_abi.CHECKS_ABI_PARAMS`, names free; an east `const` reads as
    the west one, `_canonical_type`), and with one external
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
        if (tuple(_canonical_type(ptype) for ptype, _name in fn.params)
                != tuple(ptype for ptype, _n in declared)):
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
            defined.update(_declarator_names(var.statement))
    for fn in decls.functions:
        if (fn.namespace == namespace and not fn.defined
                and not (set(fn.head.split(" ")) & _NOT_EXTERNAL_DEFINITION)):
            defined.add(fn.name)
    return [name for name in names if name not in defined]


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
    "execve", "atexit", "at_quick_exit", "asm",
)
#: Names that ALSO name ordinary things (a physics helper `open_boundary`, a local `system`): refused
#: when called, qualified (`std::rename`) or taken by address — never as a bare word.
LEAF_IO_CALL_NAMES: tuple[str, ...] = ("open", "rename", "system", "truncate")

#: What a leaf-authored source of a physics node may not contain: every file-stream and
#: stream-buffer class (`ofstream`, `fstream`, `basic_filebuf<char>`, the wide ones), anything of
#: `std::filesystem`, the names above, and the one-path `remove` (the three-argument algorithm of
#: `<algorithm>` is not a file operation). Why every leaf source and not the checks source alone
#: (round 2 of this change's review): a C++ program runs namespace-scope destructors and `atexit`
#: handlers after `main` returns — after the harness has written the run's outputs. The
#: host-rendered runner now ends with `std::_Exit`, so no such code runs (round 3); this refusal is
#: the second layer, and it also covers I/O from inside a callback. Emission is the harness's alone.
_LEAF_IO_RE = re.compile(
    r"\b\w*fstream\b|\b\w*filebuf\b|\bfilesystem\b"
    r"|\b(?:" + "|".join(LEAF_IO_NAMES) + r")\b"
    r"|(?:\bstd\s*::\s*|(?<![\w\s])\s*::\s*|&\s*)(?:" + "|".join(LEAF_IO_CALL_NAMES) + r")\b"
    r"|\b(?:" + "|".join(LEAF_IO_CALL_NAMES) + r")\s*\("
    r"|\bremove\s*\(\s*[^,()]*\)")


def _model_declaration_violations(checks_path: Path, text: str,
                                  model_files: list[Path]) -> list[str]:
    """A checks source that DECLARES one of the node's own operations (`<spec_id>__<op>` in
    namespace `<spec_id>_model`, which it must do on a `problem` node, whose host-rendered header
    declares none — round 3 of this change's review) declares it with exactly the return type and
    parameter types the model source defines it with: another spelling is an overload the model
    never defines, a link error at Build, where it would cost a whole Build to learn."""
    spec_id = checks_path.name.removesuffix("_checks.cu")
    namespace = (f"{spec_id}_model",)

    def shape(fn: cpp_decls.Function) -> tuple[str, tuple[str, ...]]:
        return fn.returns, tuple(_canonical_type(ptype) for ptype, _n in fn.params)

    defined: dict[str, set[tuple[str, tuple[str, ...]]]] = {}
    for model in model_files:
        try:
            model_text = model.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for fn in cpp_decls.read(model_text).functions:
            if fn.defined and fn.namespace == namespace:
                defined.setdefault(fn.name, set()).add(shape(fn))
    out: list[str] = []
    for fn in cpp_decls.read(text).functions:
        if fn.defined or fn.namespace != namespace or not fn.name.startswith(f"{spec_id}__"):
            continue
        if shape(fn) not in defined.get(fn.name, set()):
            out.append(
                f"{checks_path}: declares `{spec_id}_model::{fn.name}` with a return type or "
                "parameter types the model source does not define it with (or the model defines "
                "no such operation) — copy the declaration from the model's definition head "
                "exactly, or the call cannot link")
    return out


def checks_harness_isolation_violations(
    checks_path: Path, text: str, model_files: list[Path]
) -> list[str]:
    """The isolation half of the checks-source gate: neither physics source includes a harness
    header or names the harness (`harness_<x>_model`, `harness_<x>__<op>`) — the host-rendered
    runner is the only caller of the harness — and no leaf-authored source of the node (every
    `.cu` under the checks source's directory but the host-rendered runner) does file I/O, runs a
    command, or registers code to run after `main` (`_LEAF_IO_RE`). `text` is the checks
    source's raw content; every other source is read here."""
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
    violations.extend(_model_declaration_violations(checks_path, text, model_files))
    # Every leaf-authored source of the node, at any depth (`leaf_sources`), but the host-rendered
    # runner, whose name is derived from the checks source's.
    runner_name = checks_path.name.removesuffix("_checks.cu") + "_runner.cu"
    sources = [checks_path, *(p for p in leaf_sources(checks_path.parent) if p != checks_path)]
    for path in sources:
        if path.name == runner_name and path.parent == checks_path.parent:
            continue
        source = text if path == checks_path else path.read_text(encoding="utf-8",
                                                                  errors="ignore")
        io = _LEAF_IO_RE.search(cpp_lines.strip_preprocessor(cpp_lines.mask(splice_lines(source))))
        if io:
            violations.append(
                f"{path}: a physics source must not do file I/O, run a command, or register code "
                f"to run after `main` (`{io.group(0).strip()}` — no file stream, `fopen` / `open`, "
                "`std::filesystem`, `rename` / `unlink` / a one-path `remove`, `system` / `popen` "
                "/ `exec*`, `atexit`) — emission is the harness's job alone; the model computes "
                "and the checks source holds the state and computes the checks and metrics")
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
        # the call reads, so it is not an assignment statement for the "assigned before" clause.
        records.append((lhs, sources, m.start, rhs.strip(),
                        bool(pointees) or _is_declaration(body, m.start)))
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
_VIEW_BRACE_RE = re.compile(r"^(?:atmofab\s*::\s*)?View\s*<[^{}]*>\s*\{(?P<body>.*)\}$", re.DOTALL)


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


def _function_summaries(functions: list[cpp_decls.Function],
                        fixed: dict[str, Summary]) -> dict[str, Summary]:
    """`fixed` plus a summary of every function `functions` defines, computed from its BODY:
    which input parameters' data reaches each output parameter, through assignments, aliases and
    the calls it makes (round 3 of this change's review: a kernel credited from its signature
    passed although its body ignored the dependency's result). Computed to a fixed point from
    empty summaries, so a function is credited only with what its body shows, and a recursion
    terminates."""
    summaries = {**fixed, **{fn.name: {i: frozenset() for i, (ptype, _n) in enumerate(fn.params)
                                       if is_output_parameter(ptype)}
                             for fn in functions}}
    changed = True
    while changed:
        changed = False
        for fn in functions:
            records = _assignments(fn.body) + _call_records(fn.body, summaries)
            names = [name for _ptype, name in fn.params]
            current = summaries[fn.name]
            updated: Summary = {}
            for out_index in current:
                reached = _closure({names[out_index]}, records) if names[out_index] else set()
                # Any OTHER parameter whose data reaches this output, whatever its type: a
                # kernel's non-const `double* f` that it only reads is an input of that call
                # (round 5 of this change's review: filtering by type dropped it).
                updated[out_index] = frozenset(
                    i for i, name in enumerate(names)
                    if i != out_index and name and name in reached)
            if updated != current:
                summaries[fn.name] = updated
                changed = True
    return summaries


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
    `intent(out)` question, answered by the declaration. Without the header every actual is read,
    minus the Fortran binding's two further kinds: a name the file declares `const` / `constexpr`
    (not definable) and the name of a function this file defines (a procedure argument).

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
    by `_actual_names`, which falls back to every plain name the actual mentions, so an output
    reached through a pointer, a helper or a member still yields a candidate."""
    if not dep_spec_ids:
        return
    positions: dict[str, list[bool]] = {}
    for spec_id in dep_spec_ids:
        read = _dependency_out_positions(model_file, spec_id)
        if read is not None:
            positions.update(read)
    constants = {m.group("name") for m in _CONST_DECLARATION_RE.finditer(code)}
    function_names = {fn.name for fn in functions}
    summaries = _function_summaries(
        functions, {**{name: _signature_summary(out_at) for name, out_at in positions.items()},
                    **_RUNTIME_COPIES})
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
        records = _assignments(fn.body) + _call_records(fn.body, summaries)
        sources = _closure(set(outs) | returned, records)
        discarded: set[str] = set()
        for call in call_re.finditer(fn.body):
            args = _call_arguments(fn.body, call.end() - 1)
            out_at = positions.get(call.group("name"))
            candidates: set[str] = set()
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
