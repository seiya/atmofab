"""The `lint` capability of the `nvcc` linter backend (issue #289, R4-b PR-4).

WHY THE COMPILER IS THE LINTER. The linters this repository runs for its other C-family values do
not read CUDA C++: `cppcheck` 2.7 parses a kernel launch `axpy<<<1, 32>>>(...)` as a shift and
reports `shiftTooManyBits` (exit 2) on a correct source — measured 2026-09-25, and its
`--language` takes `c` / `c++` only. A false finding would be a gate a leaf cannot satisfy. What
reads CUDA C++ exactly is the CUDA compiler driver, so the declared rule set is its warnings: the
host compiler's `-Wall -Wextra` and every device-front-end warning, each promoted to an error.

THE DECLARED RULE SET IS THE FLAG LIST, and the property this backend states is the cppcheck
backend's, not fortitude's: the verdict is a function of the source, the declared flags and the
BUILD — the driver and the host compiler it invokes — bounded by `MIN_VERSION` / `BELOW_VERSION`
for the driver. The host compiler's version is NOT bounded here (it is whatever `-ccbin`
resolves to on the host; measured with g++ 11.4), so a new host compiler warning can change a
verdict; that is a stated limit, not a closed one.

THE INVOCATION TAKES FILES, NOT A DIRECTORY: the driver compiles the translation units it is
given and walks nothing, so this module declares `SOURCE_SUFFIXES` and the build-runtime server
passes every file of those suffixes under the lint target, recursively, by name
(`mcp_servers/build_runtime_server.py` `tool_run_linter`). `-Xcompiler -fsyntax-only -c` makes it
leave no artifact beside a source (measured: none in the source directory or a subdirectory).

WHAT A SOURCE CANNOT DO. An in-source suppression — `#pragma nv_diag_suppress`, `#pragma GCC
diagnostic ignored`, `_Pragma(...)` — is not something any flag of this driver disables, so it is
refused by the language backend's source gate instead
(`tools/backends/language/cuda_cpp/source.py` `preprocessor_violations`), over every
leaf-authored file this lint reads.

EXIT STATUS. Measured on 13.4 with this argv (`docs/backends/linter/nvcc/RULES.md` §Measurements):
clean 0; a finding 1, 2 or 255 (1 for an unused parameter, an unused variable (#177-D) and a
signed/unsigned comparison; 2 for a variable used before it is set (#549-D); 255 for a device
assembler (`ptxas`) error such as a kernel's shared memory over the limit — `-Xcompiler
-fsyntax-only` stops the HOST compile only, and the device code is still assembled); an unknown
flag 1 (`nvcc fatal : Unknown option`). A refused invocation and a finding therefore SHARE status 1, and the output is not read to tell them apart (a leaf names the
files in it). What makes the status a verdict is the launch self-check: the same flags are run
over an empty translation unit before the first leaf (`self_check_argv`), so a build that
refuses them is refused at launch and never reaches the gate. `unusable_invocation_reason` then
answers only for a status no run over sources produces.

Stdlib only.
"""

from __future__ import annotations

import re

#: argv[0]; the launch-time host probe reads it out of the argv this module builds.
EXECUTABLE = "nvcc"

#: The `language` values a node is linted with THIS linter for (`registry.linter_for_language`).
LANGUAGES: tuple[str, ...] = ("cuda_cpp",)

#: The suffixes of the files this linter is handed BY NAME. Its presence (not None) is what tells
#: the server this linter takes files rather than walking a directory.
SOURCE_SUFFIXES: tuple[str, ...] | None = (".cu",)

#: The language standard the lint reads a source under. Pinned rather than taken from the
#: target profile so the rule set is one declaration; the `Generate.gate` syntax stage checks the
#: source under the profile's own `toolchain.standard` and architecture.
STANDARD = "c++17"

#: The host-compiler warning groups promoted to errors.
HOST_WARNING_FLAGS: tuple[str, ...] = ("-Wall", "-Wextra", "-Werror")

#: The declared rule set and the mode, in order.
CHECK_FLAGS: tuple[str, ...] = (
    f"-std={STANDARD}",
    "-Xcompiler=" + ",".join(HOST_WARNING_FLAGS),
    "--Werror", "all-warnings",
    "-Xcompiler", "-fsyntax-only",
    "-c",
)

#: The driver versions this invocation was measured on (inclusive floor, exclusive ceiling).
MIN_VERSION: tuple[int, int, int] = (13, 0, 0)
BELOW_VERSION: tuple[int, int, int] = (14, 0, 0)
SUPPORTED_VERSION_SPEC = ">=13.0,<14.0"

#: What the probe runs to learn the installed version. The release is on the fourth line of the
#: output (`Cuda compilation tools, release 13.4, V13.4.92`), which is why `parse_version` reads
#: the `release` token rather than the first number it sees (the copyright line carries years).
VERSION_ARGV: tuple[str, ...] = (EXECUTABLE, "--version")

_VERSION_RE = re.compile(r"release\s+(\d+)\.(\d+)(?:,\s*V\d+\.\d+\.(\d+))?")


def check_argv(target: str = ".") -> tuple[str, ...]:
    """The invocation WITHOUT its sources. `target` is accepted and not read: this driver walks
    no directory, and the server appends the files it found under the target (module docstring).
    """
    del target
    return (EXECUTABLE, *CHECK_FLAGS)


def source_argv(sources: list[str]) -> tuple[str, ...]:
    """The full argv over `sources` (paths relative to the lint target, which is the cwd)."""
    return (*check_argv(), *sources)


def version_argv() -> tuple[str, ...]:
    return VERSION_ARGV


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """The (major, minor, patch) of the driver's `release` line, or `None`."""
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def _spell(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def unsupported_version_reason(version_text: str | None) -> str | None:
    """Why the installed driver must not decide a certification, or `None` when it may. Fails
    CLOSED on an unreadable version."""
    version = parse_version(version_text)
    if version is None:
        return (f"{EXECUTABLE}: could not read a release from {version_text!r} (ran "
                f"{' '.join(VERSION_ARGV)}); refused rather than trusted (supported: "
                f"{SUPPORTED_VERSION_SPEC})")
    if version < MIN_VERSION or version >= BELOW_VERSION:
        return (f"{EXECUTABLE} {_spell(version)} is outside the range the declared invocation was "
                f"measured on ({SUPPORTED_VERSION_SPEC}); re-measure and widen the range in "
                "tools/backends/linter/nvcc/lint.py rather than running unmeasured")
    return None


#: The statuses a run over sources ends in (module docstring).
VERDICT_EXIT_CODES = frozenset({0, 1, 2, 255})


def unusable_invocation_reason(returncode: int, stdout: str, stderr: str) -> str | None:
    """Why this run judged nothing, or `None` when its exit status is a verdict. Only the exit
    status is read."""
    if returncode in VERDICT_EXIT_CODES:
        return None
    return (f"{EXECUTABLE} exited {returncode}, a status no lint run over sources ends in — the "
            "invocation or the driver failed, not the source: "
            f"{(stderr or stdout).strip()[:400]}")


def self_check_argv(empty_dir: str) -> tuple[str, ...]:
    """The declared invocation over an EMPTY translation unit (`-x cu /dev/null`): exits 0 when
    this build accepts every declared flag, 1 when it refuses one (measured on 13.4)."""
    del empty_dir  # the empty translation unit needs no directory
    return (*check_argv(), "-x", "cu", "/dev/null")


def self_check_reason(returncode: int, stdout: str, stderr: str) -> str | None:
    if returncode == 0:
        return None
    return (f"{EXECUTABLE} does not accept the invocation this repository declares: it exited "
            f"{returncode} over an empty translation unit. Re-measure the invocation against "
            "this build and record it in docs/backends/linter/nvcc/RULES.md: "
            f"{(stderr or stdout).strip()[:400]}")


def lint_rules_document() -> str:
    """The declared rule set as a document a leaf can be handed."""
    return "\n".join([
        ("The static lint check compiles every CUDA C++ source with the CUDA compiler driver and "
         "fails on ANY warning; the gate runs after you return. The rule set is exactly these "
         "flags:"),
        f"  `{' '.join(check_argv())}` over each source file",
        ("- `-Wall -Wextra` of the host C++ compiler, promoted to errors by `-Werror`: every "
         "warning those two groups enable is a failure — among them an unused parameter, a "
         "comparison between signed and unsigned integers, and a `switch` over an enum that "
         "omits an enumerator without a `default`."),
        ("- `--Werror all-warnings`: every warning of the CUDA front end and device compilation "
         "is a failure — among them a variable declared but never referenced (#177-D), a "
         "variable used before its value is set (#549-D), and a non-`void` function that can "
         "reach its end without a `return` (#940-D)."),
        ("- A parameter an interface FIXES but your body never reads keeps its name (the "
         "published signature pins it) and is marked read with `(void)name;` as the first "
         "statement of the body."),
        ("- An in-source suppression (`#pragma nv_diag_suppress`, `#pragma diag_suppress`, "
         "`#pragma GCC diagnostic`, `#pragma clang diagnostic`, `_Pragma(...)`) is refused "
         "outright by a separate deterministic check, so it suppresses nothing — fix the finding "
         "instead."),
    ])
