"""How the deterministic `Generate` gates read a CUDA C++ source (issue #289, R4-b PR-4).

The `source_reading` capability. Every reader here works on `lines.mask` of the text, so a comment
or the inside of a literal never answers a question about code.

WHAT IS IMPLEMENTED, AND WHAT IS REFUSED. This backend serves the node that R4-b builds for the
`cpp_gpu` target: the `infrastructure` harness, whose leaf authors both the model and the runner.
The gates such a node reaches are implemented in full: the build control file's source facts
(`MODULE_SOURCE_SUFFIXES`, `source_module_deps`), the runner scans
(`validate_runner_json_serialization`, `validate_runner_snapshot_filenames`), the model gates
every node gets (`model_source_gates`: the literal-metric floors and the preprocessor
allowlist), the loop count the parallel presence floor reads (`counted_loops`), and the absent-model
remedy. The gates only a PHYSICS node reaches — the checks module's ABI facts and isolation, the
`problem` model gates, the dependency-call gates — are a later change's (the runner this language
renders over the harness does not exist yet, so neither does the checks ABI it would call). A
`cuda_cpp` physics node is refused at LAUNCH for exactly that reason (it declares no
`runner_render`), and each of those functions here is a REFUSAL rather than a pass, so a caller
that reaches one anyway is told why instead of certifying a source nothing read.

Stdlib only; imports nothing from the neutral core.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from tools.backends.language.cuda_cpp import declarations as cpp_decls
from tools.backends.language.cuda_cpp import lines as cpp_lines

_PHYSICS_REFUSAL = (
    "reading this gate's facts from a CUDA C++ source is not implemented yet: the gate belongs to "
    "a physics node (a `component` or `problem` node, whose runner the host renders over the "
    "harness), and the `cuda_cpp` language backend does not render one, so such a node is "
    "refused at launch (`target_profile.toolchain_servable_reasons`: no `runner_render`) — "
    "refused here too rather than passed unread")


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


def splice_lines(text: str) -> str:
    """`text` with every backslash-newline removed — the translation phase that joins a continued
    line before any directive or token is recognized."""
    return re.sub(r"\\\r?\n", "", text)


def preprocessor_violations(path: Path, text: str) -> list[str]:
    """The allowlist above, over the CODE of `text` after line splicing (a directive or a token
    inside a comment or a literal is not one). Line numbers are those of the spliced text."""
    code = cpp_lines.mask(splice_lines(text))
    out: list[str] = []
    for m in _DIRECTIVE_RE.finditer(code):
        body = m.group("body").strip()
        if not _ALLOWED_DIRECTIVE_RE.match(body):
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
    a file the model or runner includes reaches the gates through it. A physics node's model gates
    are refused (module docstring)."""
    del dep_spec_ids, multidim_spec_id  # read by the physics gates only
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
    if not node_key.startswith("infrastructure/"):
        violations.append(f"{model_file}: {_PHYSICS_REFUSAL}")


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
    calls = [(m.end() - 1, _close_paren(code, m.end() - 1)) for m in _PRINTF_CALL_RE.finditer(code)]
    for start, _end, literal in cpp_lines.literal_spans(text):
        if not any(open_at < start < close_at for open_at, close_at in calls):
            continue  # not an argument of a printf-family call: not a format
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


# --- the physics-node gates (refused; module docstring) -----------------------------------------

def checks_module_declaration_violations(checks_path: Path, text: str, spec_id: str) -> list[str]:
    del text, spec_id
    return [f"{checks_path}: {_PHYSICS_REFUSAL}"]


def checks_harness_isolation_violations(
    checks_path: Path, text: str, model_files: list[Path]
) -> list[str]:
    del text, model_files
    return [f"{checks_path}: {_PHYSICS_REFUSAL}"]


def checks_module_abi_facts(text: str, spec_id: str) -> tuple[set[str], set[str], set[str]]:
    """Nothing published: every checks-ABI name reads as unpublished, so a caller refuses."""
    del text, spec_id
    return set(), set(), set()


def unpublished_bound_state(text: str, spec_id: str, bound: Iterable[str]) -> list[str]:
    """Every bound name reads as unpublished, so a caller refuses."""
    del text, spec_id
    return list(bound)


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


def validate_dependency_operations(
    model_files: list[Path],
    dep_spec_ids: list[str],
    violations: list[str],
) -> None:
    """A node with dependencies is a physics node here: refused (module docstring). A node with
    none — the harness — has nothing to check."""
    if dep_spec_ids:
        target = model_files[0] if model_files else Path(".")
        violations.append(f"{target}: {_PHYSICS_REFUSAL}")
