"""The `device_trace` capability of the CUDA parallel backend (issue #307).

The one place this repository spells Nsight Systems: how a binary is run under it, how its
per-kernel summary is asked for and what file that writes, which programs the site that executes
the binary needs, and how the summary and a CUDA C++ source are read for kernel names. The neutral
core holds what this returns as opaque tokens (`tools/host_execution.py`: an argv prefix, a summary
argv, a file name) and the name -> instance-count mapping the reader returns.

Measured (issue #307 plan §0-2 on Nsight Systems 2026.3.2; comment 5851969504 on the `cpp_gpu`
site's 2025.1.3), and `docs/backends/parallel/cuda/DEVICE_TRACE.md` states it for a reader:

* `nsys profile … -o <stem>` returns the application's exit code and writes `<stem>.nsys-rep`
  relative to its cwd.
* `nsys stats -r cuda_gpu_kern_sum -f csv -o <stem> …` writes `<stem>_cuda_gpu_kern_sum.csv`. A
  report holding no kernel data (no launch reached the device) gives an EMPTY file and exit 0, so
  the exit code says nothing about kernels. `--force-overwrite=true` replaces a writable file
  already at that path (a forged one was replaced on both versions) and NOT a read-only one,
  which survives with exit 0 — hence `summary_argv` empties the path first.
* A report that is not readable (truncated, or not a report) also exits 0 with an empty summary,
  and writes no `<stem>.sqlite`; a readable one writes it, with kernel data or without — on
  2025.1.3 too, measured at the site with `summary_argv` itself (issue #307 comment 5852641160).
  `summary_argv` requires it.
* The CSV's columns are `Time (%)`, `Total Time (ns)`, `Instances`, `Avg (ns)`, `Med (ns)`,
  `Min (ns)`, `Max (ns)`, `StdDev (ns)`, `Name`. `Name` is demangled: `k(double *, long)`,
  `ns::k(int *)`, one row per template instantiation `void k<double>(T1 *)`. A kernel that never
  ran has no row.

Stdlib only, plus the language backend's masking asked through the registry.
"""

from __future__ import annotations

import csv
import io
import re

#: The programs the site that executes the binary must resolve
#: (`host_prerequisites.execution_executables` adds them to the launch probe).
EXECUTABLES: tuple[str, ...] = ("nsys",)

_REPORT_SUFFIX = ".nsys-rep"
_SUMMARY_REPORT = "cuda_gpu_kern_sum"
_NAME_COLUMN = "Name"
_INSTANCES_COLUMN = "Instances"


def profile_argv_prefix(stem: str) -> tuple[str, ...]:
    """The argv the binary runs under: a CUDA trace written to `<stem>.nsys-rep` in the cwd,
    replacing one already there. The application's own exit code is what this returns."""
    return ("nsys", "profile", "-t", "cuda", "-o", stem, "--force-overwrite=true")


#: The summary command's frame around the stats run. `$1` is the summary's path, `$2` the export
#: database's; the rest is the stats argv. Both paths are emptied first (failing when that cannot
#: be done); after the stats run exits 0 the export database must exist, since the stats run
#: writes it for any readable report and for no other (measured below), and its exit code does
#: not tell the two apart.
_FRESH_OUTPUT_SCRIPT = (
    'out=$1; db=$2; rm -f -- "$out" "$db" || exit 1; shift 2; "$@" || exit; '
    '[ -s "$db" ] || { echo "kernel-trace-summary: the report was not exported: $db is '
    'missing" >&2; exit 3; }')


def summary_argv(stem: str) -> tuple[str, ...]:
    """The command that reads `<stem>.nsys-rep` in the cwd and writes the per-kernel summary
    (`summary_file(stem)`). `--force-export=true` rebuilds the export database (`<stem>.sqlite`)
    rather than reusing one already there.

    Two things the stats run's exit code does not say, both measured on 2026.3.2, are said by
    the frame around it (`_FRESH_OUTPUT_SCRIPT`):

    * A READ-ONLY file at the summary's path survives the stats run, which prints
      `ERROR: Unable to open output file for writing` and exits 0 — so the file read afterwards
      would be whatever the binary left there (issue #307 PR-2 round 1). The summary's path is
      emptied first, and a path that cannot be emptied fails the command.
    * A report that is not a readable one (truncated, or any other bytes) exits 0 with an EMPTY
      summary — the shape of "no kernel data" — and writes no export database; a readable report
      writes one, with kernel data or without (round 2). The export database's path is emptied
      first too, and a stats run after which it does not exist fails the command (exit 3).

    Done in the command rather than by the host so that one argv carries it to both the local
    run and the remote job, where no host step runs between the two commands."""
    return ("sh", "-c", _FRESH_OUTPUT_SCRIPT, "kernel-trace-summary", summary_file(stem),
            f"{stem}.sqlite", "nsys", "stats", "-r", _SUMMARY_REPORT, "-f", "csv", "-o", stem,
            "--force-export=true", "--force-overwrite=true", f"{stem}{_REPORT_SUFFIX}")


def summary_file(stem: str) -> str:
    """The file `summary_argv(stem)` writes, relative to its cwd (measured: `-o X` writes
    `X_cuda_gpu_kern_sum.csv`)."""
    return f"{stem}_{_SUMMARY_REPORT}.csv"


class SummaryUnreadable(ValueError):
    """A non-empty summary this reader cannot read: a header without the two columns it reads,
    or an instance count that is not a non-negative integer (an Nsight Systems whose CSV shape
    drifted from the measured one)."""


def kernel_instances(text: str) -> dict[str, int] | None:
    """The summary `text` as base kernel name -> summed instance count, or `None` when it records
    no kernel: an empty (or blank) file, which is what a report with no kernel data gives
    (measured), or a header with no row. Rows of one base name are summed — a template kernel has
    one row per instantiation. Raises `SummaryUnreadable` for a shape it cannot read."""
    if not text.strip():
        return None
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    missing = [c for c in (_NAME_COLUMN, _INSTANCES_COLUMN) if c not in fields]
    if missing:
        raise SummaryUnreadable(
            f"the kernel summary's header {fields!r} has no column(s) {missing!r}")
    counts: dict[str, int] = {}
    for row in reader:
        name = (row.get(_NAME_COLUMN) or "").strip()
        raw = (row.get(_INSTANCES_COLUMN) or "").strip()
        if not name or not raw.isdigit():
            raise SummaryUnreadable(
                f"the kernel summary has a row without a kernel name and a non-negative integer "
                f"instance count: {row!r}")
        base = base_kernel_name(name)
        counts[base] = counts.get(base, 0) + int(raw)
    return counts or None


def _strip_trailing_group(text: str, open_ch: str, close_ch: str) -> str:
    """`text` without one balanced `open_ch … close_ch` group at its end, if it ends with one."""
    text = text.rstrip()
    if not text.endswith(close_ch):
        return text
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        if text[i] == close_ch:
            depth += 1
        elif text[i] == open_ch:
            depth -= 1
            if depth == 0:
                return text[:i].rstrip()
    return text


def base_kernel_name(demangled: str) -> str:
    """The unqualified name of a demangled kernel: `k(double *, long)` -> `k`, `ns::k(int *)` ->
    `k`, `void k<double>(T1 *)` -> `k`, `(anonymous namespace)::k(int *)` -> `k`. Removes the
    trailing parameter list and template argument list (each matched from the end, so a
    parenthesis inside a qualifier is not taken for them), then keeps the last blank-separated
    word and its last `::` segment."""
    name = _strip_trailing_group(demangled.strip(), "(", ")")
    name = _strip_trailing_group(name, "<", ">")
    words = name.split()
    last = words[-1] if words else name
    return last.rsplit("::", 1)[-1]


# Attributes spelled `__name__(…)` that CUDA reads, BY NAME — a kernel may itself be named
# `__k__` (round 2 of PR-2: a pattern for any `__name__(` blanked that kernel) — and the GNU form.
# Each is blanked with its whole balanced argument list, so its parenthesis is not taken for a
# parameter list.
_PAREN_ATTRIBUTES = ("__attribute__", "__launch_bounds__", "__cluster_dims__", "__maxnreg__")
_PAREN_ATTRIBUTE_RE = re.compile(r"\b(" + "|".join(_PAREN_ATTRIBUTES) + r")\s*\(")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_KERNEL_MARK = "__global__"
_KERNEL_MARK_RE = re.compile(r"\b__global__\b")
_OPEN = {"(": ")", "[": "]", "{": "}"}


def _balanced_end(code: str, at: int) -> int:
    """The index just past the bracket group opening at `code[at]` (`(`, `[` or `{`), nested
    groups of any of the three kinds included; `len(code)` when it does not close."""
    stack: list[str] = []
    for i in range(at, len(code)):
        ch = code[i]
        if ch in _OPEN:
            stack.append(_OPEN[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return i + 1
    return len(code)


def _names_global(attribute_list: str) -> bool:
    """Whether a comma-separated attribute list names the `global` attribute: some item whose
    NAME (the part before its argument list, last `::` segment, underscores stripped) is
    `global`. An argument that merely mentions `global` (`[[gnu::aligned(global)]]`) is not."""
    item, depth, items = [], 0, []
    for ch in attribute_list:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(item))
            item = []
        else:
            item.append(ch)
    items.append("".join(item))
    for it in items:
        head = it.split("(", 1)[0].strip()
        if head and head.rsplit("::", 1)[-1].strip().strip("_") == "global":
            return True
    return False


def _mark_attributes(code: str) -> str:
    """`code` with every attribute replaced, at its own length, by `__global__` when it names
    the `global` attribute and by blanks otherwise. nvcc makes a kernel of a function declared
    `[[gnu::global]]`, `[[ gnu :: global ]]`, `[[gnu::global, gnu::noinline]]` or
    `__attribute__((global))` (each measured: an entry symbol in the object), none of which
    carries the keyword — so here the attribute, not the keyword, is what marks a kernel (issue
    #307 PR-3 round 1). Each attribute is matched with its balanced brackets, so an argument
    carrying its own brackets (`gnu::aligned(alignof(int[1]))`) does not end it early (round 2).
    Every attribute naming `global` is at least as long as the mark (`[[global]]` is 10)."""
    out = list(code)
    i = 0
    while i < len(code):
        if code.startswith("[[", i):
            end = _balanced_end(code, i)
            inner = code[i + 2:max(i + 2, end - 2)]
        else:
            match = _PAREN_ATTRIBUTE_RE.match(code, i)
            if match is None or (i and (code[i - 1].isalnum() or code[i - 1] == "_")):
                i += 1
                continue
            end = _balanced_end(code, match.end() - 1)
            inner = code[match.end():end - 1]
            if match.group(1) == "__attribute__":
                inner = inner.strip()
                inner = inner[1:-1] if inner.startswith("(") and inner.endswith(")") else inner
            else:
                inner = ""
        span = end - i
        mark = _KERNEL_MARK if _names_global(inner) and span >= len(_KERNEL_MARK) else ""
        out[i:end] = mark + " " * (span - len(mark))
        i = end
    return "".join(out)


def _declarator_name(code: str, mark_start: int, mark_end: int) -> str | None:
    """The name of the function a kernel mark belongs to: the declarator before the first
    parameter list that follows the mark in the same declaration. The mark may stand anywhere
    before that list — ahead of the return type, between it and the name, or between the name
    and the list (`void k __global__ (int*)`, `auto k [[gnu::global]] (int*) -> void`; each
    measured to be a kernel on nvcc, round 2) — so the name is read back from the list, not
    forward from the mark. A parenthesized name (`__global__ void (k)(int*)`) is the one
    inside. None when no parameter list follows before the declaration ends."""
    k = mark_end
    while k < len(code) and code[k] not in "(;{}":
        k += 1
    if k >= len(code) or code[k] != "(":
        return None
    close = _balanced_end(code, k)
    inner = code[k + 1:close - 1].strip()
    rest = code[close:].lstrip()
    if _IDENTIFIER_RE.fullmatch(inner) and inner != _KERNEL_MARK and rest.startswith("("):
        return inner
    start = max(code.rfind(c, 0, mark_start) for c in ";{}") + 1
    head = _strip_trailing_group(code[start:k], "<", ">")
    names = [t for t in _IDENTIFIER_RE.findall(head) if t != _KERNEL_MARK]
    return names[-1] if names else None


def defined_kernels(text: str) -> tuple[str, ...]:
    """The names of the `__global__` functions the CUDA C++ source `text` defines or declares, in
    order, each once. Read over the language backend's code view (comments and literal contents
    masked, so a `__global__` in a comment or a string is not one), with every attribute naming
    `global` read as the keyword and every other attribute blanked (`_mark_attributes`), and each
    mark resolved to its declarator (`_declarator_name`)."""
    from tools.backends import registry
    reader = registry.capability_module("language", "cuda_cpp", "source_reading")
    code = _mark_attributes(reader.code_view(text))
    names: list[str] = []
    for match in _KERNEL_MARK_RE.finditer(code):
        name = _declarator_name(code, match.start(), match.end())
        if name is not None and name not in names:
            names.append(name)
    return tuple(names)
