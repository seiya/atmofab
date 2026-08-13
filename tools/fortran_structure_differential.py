#!/usr/bin/env python3
"""Development harness: does the Fortran structure front end read this tree the way it should?

Two independent halves, neither of which contributes to any verdict:

* ``--tree`` prints, for every ``*_model.f90`` in this checkout, the three ``problem`` model
  gates' violations and the envelope tuples they were computed from. It answers "did this change
  move anything" only by being run at TWO revisions and diffed — that is the point of printing a
  stable, line-oriented listing rather than a judgement.
* ``--flang`` cross-checks the front end against ``flang -fc1 -fdebug-unparse-no-sema``, whose
  canonicalised output (one statement per line, KEYWORDS upper-cased while user identifiers keep
  their spelling) makes ``END SUBROUTINE`` and a variable named ``endsubroutine`` different
  tokens. It was measured byte-identical across LLVM 17/18/19/21 on this corpus, which is what
  makes it usable as an oracle. It stays OUT of the gate deliberately: a verdict must not depend
  on which machine ran it, and flang is not a dependency this toolchain can require (``gfortran``
  is). See the DECIDED note in ``TODO.md``.

THIS IS NOT A SUITE TEST, and it does not skip. Both halves check their prerequisites up front and
EXIT NON-ZERO when one is missing, because the calibration lesson of this repository is that a
check which quietly skips is a check that has stopped running. Run it by hand when the front end,
its query set, or the pinned package versions change.

Enumeration uses ``os.walk``, never ``grep -r``: in an interactive shell here ``grep`` is a
ripgrep alias that honours ``.gitignore``, and the workspaces holding 364 of the 365 models are
ignored — a first pass once reported 1 file and drew a conclusion from it.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import fortran_structure  # noqa: E402
from tools.validate_pipeline_semantics import (  # noqa: E402
    NodeExecution,
    _fortran_subroutine_envelopes,
    _FortranSourceStructureError,
    _validate_problem_metric_only_scalar_kernel,
    _validate_problem_model_dependency_dataflow,
    _validate_problem_model_literal_outputs,
)

#: The candidates in the order the DECIDED note measured them. `flang-new` is the LLVM 17-19
#: spelling and `flang` / `flang-21` the LLVM 21 one, which is exactly the distribution problem
#: that keeps this binary out of the gate.
_FLANG_CANDIDATES = ("flang-21", "flang", "flang-new-19", "flang-new-18", "flang-new-17", "flang-new")

_USE_MODEL_PATTERN = re.compile(r"^\s*use\s+([a-z_][a-z0-9_]*)_model\b", re.MULTILINE)


def model_files() -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        found += [Path(dirpath) / name for name in filenames if name.endswith("_model.f90")]
    return sorted(found)


def dep_spec_ids_of(lowered: str) -> list[str]:
    """The dependency spec ids this file itself names, via its own `use <spec_id>_model` lines.

    Read from the file rather than from a pipeline's IR so the harness covers every model in the
    tree, including the ones whose pipeline is long gone.
    """
    return sorted(set(_USE_MODEL_PATTERN.findall(lowered)))


def gate_violations(path: Path, lowered: str) -> list[str]:
    """The three `problem` gates' violations for one file, under a forced `problem/` node key.

    Forced because every one of the three returns early for a non-`problem` node, and a
    differential that exercised only the real node kinds would be blind on most of the corpus.
    The spec id comes from the FILE NAME (`<spec_id>_model.f90`) rather than being a constant:
    the metric-only gate only looks at a node whose spec id names `2d` / `3d`, so a constant key
    would silently reduce that gate to a no-op over the whole corpus — which is exactly the shape
    of the silent check this harness exists to catch.
    """
    spec_id = path.name[: -len("_model.f90")]
    execution = NodeExecution(
        node_key=f"problem/{spec_id}@0.0.0",
        node_dir=path.parent,
        exec_dir=path.parent,
        pipeline_dir=path.parent,
    )
    violations: list[str] = []
    envelopes = _fortran_subroutine_envelopes(lowered)
    _validate_problem_model_literal_outputs(
        execution=execution, model_file=path, envelopes=envelopes, violations=violations)
    _validate_problem_model_dependency_dataflow(
        execution=execution, model_file=path, lowered=lowered, envelopes=envelopes,
        dep_spec_ids=dep_spec_ids_of(lowered), violations=violations)
    _validate_problem_metric_only_scalar_kernel(
        execution=execution, model_file=path, envelopes=envelopes, violations=violations)
    return violations


def run_tree() -> int:
    files = model_files()
    print(f"# files: {len(files)}")
    envelope_count = 0
    refused: list[Path] = []
    counts = {"literal": 0, "dataflow": 0, "metric": 0}
    for path in files:
        relative = path.relative_to(REPO_ROOT)
        lowered = path.read_text(encoding="utf-8", errors="replace").lower()
        try:
            envelopes = _fortran_subroutine_envelopes(lowered)
        except _FortranSourceStructureError as exc:
            refused.append(path)
            for error in exc.errors:
                print(f"REFUSED {relative} line={error.line} missing={error.missing} "
                      f"{error.snippet!r}")
            continue
        for envelope in envelopes:
            envelope_count += 1
            print(f"ENVELOPE {relative} name={envelope.name} args={envelope.dummy_args!r} "
                  f"body_len={len(envelope.body)} out_scope_len={len(envelope.out_scope)} "
                  f"out_vars={sorted(envelope.out_vars)}")
        for violation in gate_violations(path, lowered):
            text = violation.replace(str(REPO_ROOT) + "/", "")
            print(f"VIOLATION {text}")
            if "literal-only assignments" in violation:
                counts["literal"] += 1
            elif "metric-only scalar kernel" in violation:
                counts["metric"] += 1
            else:
                counts["dataflow"] += 1
    print(f"# envelopes: {envelope_count}")
    print(f"# refused files: {len(refused)}")
    print(f"# violations: literal-outputs={counts['literal']} "
          f"dependency-dataflow={counts['dataflow']} metric-only-kernel={counts['metric']}")
    return 0


def find_flang() -> str:
    for candidate in _FLANG_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    print(
        "fortran_structure_differential: no flang found (tried "
        f"{', '.join(_FLANG_CANDIDATES)}). This half is a DEVELOPMENT requirement and does not "
        "skip: install LLVM flang (e.g. `apt install flang-21`) or run only --tree.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def flang_unparse(binary: str, source: str, workdir: Path) -> str | None:
    """`flang -fc1 -fdebug-unparse-no-sema` over ``source``, or None when flang refuses it.

    `-no-sema` is the half that matters: a gate must read code that fails semantic analysis, and
    plain `-fdebug-unparse` returns nothing there.
    """
    path = workdir / "unit.f90"
    path.write_text(source)
    result = subprocess.run(
        [binary, "-fc1", "-fdebug-unparse-no-sema", str(path)],
        cwd=workdir, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout


# `(?!END\b)`: flang writes the terminator as `END SUBROUTINE name`, and `END` is otherwise
# indistinguishable from the prefix words (`PURE`, `ELEMENTAL`, `MODULE`, …) this allows before
# the keyword. Without it every definition was counted twice and all 360 files "disagreed".
_FLANG_SUBROUTINE = re.compile(r"^\s*(?!END\b)(?:[A-Z_]+ )*SUBROUTINE ([A-Za-z_][A-Za-z0-9_]*)")
_FLANG_FUNCTION = re.compile(r"^\s*(?!END\b)(?:[A-Z_]+ )*FUNCTION ([A-Za-z_][A-Za-z0-9_]*)")
_FLANG_INTERFACE_OPEN = re.compile(r"^\s*(?:ABSTRACT )?INTERFACE\b", re.MULTILINE)
_FLANG_INTERFACE_END = re.compile(r"^\s*END INTERFACE\b", re.MULTILINE)


def flang_subroutine_names(unparsed: str) -> list[str]:
    """The subroutine DEFINITION names flang's canonical output shows, interface bodies excluded.

    Deliberately naive — it is an ORACLE, not a second implementation of the gate: flang has
    already done the hard half (deciding what is a keyword), so a line-oriented reading of its
    output is sound in a way the same reading of raw source is not.
    """
    names: list[str] = []
    depth = 0
    for line in unparsed.splitlines():
        if _FLANG_INTERFACE_END.match(line):
            depth = max(depth - 1, 0)
            continue
        if _FLANG_INTERFACE_OPEN.match(line):
            depth += 1
            continue
        if depth:
            continue
        match = _FLANG_SUBROUTINE.match(line)
        if match:
            names.append(match.group(1).lower())
    return names


def run_flang() -> int:
    binary = find_flang()
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False)
    print(f"# flang: {binary}")
    print("# " + version.stdout.strip().splitlines()[0] if version.stdout.strip() else "# (no version)")
    files = model_files()
    workdir = Path(tempfile.mkdtemp(prefix="fortran-structure-differential-"))
    agreed = disagreed = unparseable = refused = 0
    try:
        for path in files:
            source = path.read_text(encoding="utf-8", errors="replace")
            unparsed = flang_unparse(binary, source, workdir)
            if unparsed is None:
                unparseable += 1
                print(f"FLANG-REFUSED {path.relative_to(REPO_ROOT)}")
                continue
            try:
                envelopes = _fortran_subroutine_envelopes(source.lower())
            except _FortranSourceStructureError:
                refused += 1
                print(f"TREE-REFUSED {path.relative_to(REPO_ROOT)}")
                continue
            ours = sorted(envelope.name for envelope in envelopes)
            theirs = sorted(flang_subroutine_names(unparsed))
            if ours == theirs:
                agreed += 1
            else:
                disagreed += 1
                print(f"DISAGREE {path.relative_to(REPO_ROOT)}\n  tree ={ours}\n  flang={theirs}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"# agreed: {agreed}; disagreed: {disagreed}; "
          f"flang-refused: {unparseable}; tree-refused: {refused}")
    return 1 if disagreed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", action="store_true", help="print gate violations + envelopes")
    parser.add_argument("--flang", action="store_true", help="cross-check against flang")
    args = parser.parse_args()
    if not args.tree and not args.flang:
        parser.error("choose --tree, --flang, or both")
    # Fail before doing any work if the front end itself is missing: an empty listing must never
    # be mistaken for "nothing moved".
    try:
        fortran_structure.parse_view("subroutine probe\nend subroutine\n")
    except fortran_structure.FortranStructureUnavailableError as exc:
        print(f"fortran_structure_differential: {exc}", file=sys.stderr)
        return 2
    status = 0
    if args.tree:
        status |= run_tree()
    if args.flang:
        status |= run_flang()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
