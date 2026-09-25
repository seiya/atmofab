#!/usr/bin/env python3
"""The `make` build system's `control_file` capability (issue #289, R4-b PR-3).

Two halves, both moved unchanged out of the neutral core:

* the RENDERERS of the host-authored `src/Makefile` — `render_node` (the IR-shaped Makefile of a
  node with a fixed model / checks / runner set, and the Model B dependency closure) and
  `render_from_graph` (a pure `CodegenBundle`'s derived build graph), from
  `tools/workflow_conductor.py`. The GRAMMAR is this module's; what it says to compile a language
  comes from that language backend's `control_file` rules (`rules`), which the conductor hands in;
* the GATES over a Makefile (`gates.py`, over the `parse.py` reader), from
  `tools/validate_pipeline_semantics.py`, and the classification of a failed build's output
  (`failure.py`), from the conductor.

`CONTROL_FILE_BASENAME` is the file the build system reads.

Stdlib only; imports nothing from the neutral core.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from tools.backends.build_system.make.failure import (  # noqa: F401  (re-export)
    classify_build_failure,
)
from tools.backends.build_system.make.gates import (  # noqa: F401  (re-export)
    validate_bin_overridable,
    validate_src_dir,
    validate_test_invokes_cases,
    validate_test_no_relink,
)

#: The control file `make` reads, relative to the source directory it is run in.
CONTROL_FILE_BASENAME = "Makefile"

#: `make` builds in the source tree it is run in (`compile_project` / `run_quality_checks` take
#: `project_dir=<pipeline>/source/<source_id>/src/`), so its command logs land beside the control
#: file — the one cross-phase log placement the runtime grants
#: (`orchestration_runtime._builds_in_source`).
BUILDS_IN_SOURCE = True

#: The `run_quality_checks` presets that run this build system's test target, each mapped to the
#: target it requires the control file to declare. A compiled language's quality check must use
#: one of them (`validate_pipeline_semantics._validate_quality_check_commands`).
QUALITY_CHECK_PRESETS: dict[str, str] = {"make_test": "test", "make_check": "check"}


def _clean_recipe(rules: dict[str, Any]) -> str:
    artifacts = " ".join(f"$(OBJDIR)/{glob}" for glob in ("*.o", *rules["compile_artifact_globs"]))
    return f"\trm -f {artifacts} $(BINDIR)/$(BIN)\n"


def _compile(rules: dict[str, Any]) -> str:
    return f"$({rules['compiler_variable']}) $({rules['flags_variable']})"


def _padded(variable: str) -> str:
    """`FC      ` — the variable name padded to the column the assignments align on."""
    return f"{variable:<8}"


def render_node(
    *,
    rules: dict[str, Any],
    compiler: str,
    bin_name: str,
    cases_default: str,
    model_stem: str,
    runner_stem: str,
    checks_stem: str | None,
    closure: Iterable[str],
) -> str:
    """The IR-shaped Makefile: a node's model and runner (and, on a harness-backed physics node,
    its checks module between them: model.o <- checks.o <- runner.o), plus the Model B dependency
    closure `closure` (dependency spec_ids, deepest first) whose sources the conductor stages into
    `$(OBJDIR)` before make. An empty closure emits the leaf template byte for byte.

    Imposes `BIN ?= <bin_name>` (overridable so Build / Validate.execute can pin the canonical
    binary name); OBJDIR / BINDIR / RUNDIR default to "." and are overridden by Build
    (`compile_project`) and Validate.execute (`run_quality_checks`); SPEC / CASES default so a
    local `make all test` runs the full case set standalone, and Validate.execute overrides them
    through the make-test environment so `make test` invokes the runner as `run_program` does."""
    closure = list(closure)
    suffix = rules["source_suffix"]
    cc = _compile(rules)
    dep_objs_line = ""
    dep_rules = ""
    model_dep_prereq = ""
    link_dep_prereq = ""
    if closure:
        dep_objs = " ".join(f"$(OBJDIR)/{d}_model.o" for d in closure)
        dep_objs_line = f"\nDEP_OBJS = {dep_objs}\n"
        model_dep_prereq = " $(DEP_OBJS)"
        link_dep_prereq = "$(DEP_OBJS) "
        # Deepest-first: each dep object depends on all deeper dep objects so the module
        # artifacts they provide exist first (conservative over-ordering — safe for correctness).
        parts = []
        for i, d in enumerate(closure):
            deeper = " ".join(f"$(OBJDIR)/{closure[j]}_model.o" for j in range(i))
            deeper = (deeper + " ") if deeper else ""
            parts.append(
                f"$(OBJDIR)/{d}_model.o: $(OBJDIR)/{d}_model{suffix} {deeper}| $(OBJDIR)\n"
                f"\t{cc} -c $(OBJDIR)/{d}_model{suffix} -o $(OBJDIR)/{d}_model.o\n")
        dep_rules = "\n" + "\n".join(parts)

    # The checks-module blocks (empty for a node without one -> the template is emitted byte for
    # byte as before). checks.o `use`s the model, so it depends on MODEL_OBJ; the runner links
    # against it, so it is a runner prereq + link input.
    has_checks = checks_stem is not None
    checks_src_decl = f"CHECKS_SRC = {checks_stem}{suffix}\n" if has_checks else ""
    checks_obj_decl = f"CHECKS_OBJ = $(OBJDIR)/{checks_stem}.o\n" if has_checks else ""
    checks_prereq = "$(CHECKS_OBJ) " if has_checks else ""
    checks_rule = (
        "$(CHECKS_OBJ): $(CHECKS_SRC) $(MODEL_OBJ) | $(OBJDIR)\n"
        f"\t{cc} -c $(CHECKS_SRC) -o $(CHECKS_OBJ)\n\n"
        if has_checks else "")
    pin_comment = "".join(f"# {line}\n" for line in rules["compiler_pin_comment"])

    return f"""\
# Deterministic Makefile authored by the conductor (build_system=make, language={rules['language']}).
# Out-of-source capable: OBJDIR/BINDIR/RUNDIR default to "." and are overridden by
# Build (compile_project) and Validate.execute (run_quality_checks).

{pin_comment}# The dirs/BIN stay ?= because Build/Validate.execute inject them via command line / env.
# SPEC/CASES stay ?= because Validate.execute injects them via the make-test env so the
# `make test` re-run invokes the runner identically to run_program (`--cases <spec> <ids>`);
# the ?= defaults keep a local `make all test` runnable standalone.
{_padded(rules['compiler_variable'])}:= {compiler}
OBJDIR  ?= .
BINDIR  ?= .
RUNDIR  ?= .
{_padded(rules['flags_variable'])}?= {rules['flags']}

BIN ?= {bin_name}
SPEC ?= spec.ir.yaml
CASES ?= {cases_default}

MODEL_SRC  = {model_stem}{suffix}
{checks_src_decl}RUNNER_SRC = {runner_stem}{suffix}

MODEL_OBJ  = $(OBJDIR)/{model_stem}.o
{checks_obj_decl}RUNNER_OBJ = $(OBJDIR)/{runner_stem}.o
{dep_objs_line}
.PHONY: all test clean
.DEFAULT_GOAL := all

all: $(BINDIR)/$(BIN)
{dep_rules}
$(MODEL_OBJ): $(MODEL_SRC){model_dep_prereq} | $(OBJDIR)
\t{cc} -c $(MODEL_SRC) -o $(MODEL_OBJ)

{checks_rule}$(RUNNER_OBJ): $(RUNNER_SRC) {checks_prereq}$(MODEL_OBJ) | $(OBJDIR)
\t{cc} -c $(RUNNER_SRC) -o $(RUNNER_OBJ)

$(BINDIR)/$(BIN): {link_dep_prereq}$(MODEL_OBJ) {checks_prereq}$(RUNNER_OBJ) | $(BINDIR)
\t{cc} {link_dep_prereq}$(MODEL_OBJ) {checks_prereq}$(RUNNER_OBJ) -o $(BINDIR)/$(BIN)

# $(sort ...) dedups the target list: when OBJDIR==BINDIR (in-source make, both ".")
# it collapses to a single target, avoiding the harmless `target '.' given more than
# once` warning (and without two recipes for the same target).
$(sort $(OBJDIR) $(BINDIR)):
\tmkdir -p $@

test:
\ttest -x $(BINDIR)/$(BIN) || {{ echo "error: $(BINDIR)/$(BIN) not built; run 'make all' first" >&2; exit 1; }}
\tmkdir -p $(RUNDIR)/raw/state_snapshots/initial
\tcd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)

clean:
{_clean_recipe(rules)}"""


def render_from_graph(
    *,
    rules: dict[str, Any],
    compiler: str,
    bin_name: str,
    cases_default: str,
    graph: dict[str, Any],
) -> str:
    """The Makefile of a pure `CodegenBundle`: EXACTLY the derived build graph's
    `compile_units` (so a bundle that declares a helper / internal-module file is built too) and
    its link objects. The overridable surface and the test / clean targets match `render_node`,
    so Build and Validate.execute drive both identically. A `staged:` source is
    `$(OBJDIR)/<name>` (staged by the conductor before make), a `bundle:` / `glue:` source a
    filename in the src/ cwd; objects live under `$(OBJDIR)`, in the graph's conservative total
    prerequisite order."""
    cc = _compile(rules)

    def _src_path(source: str) -> str:
        kind, _, name = source.partition(":")
        return f"$(OBJDIR)/{name}" if kind == "staged" else name

    compile_units = graph.get("compile_units") or []
    unit_rules: list[str] = []
    for unit in compile_units:
        src = _src_path(str(unit.get("source", "")))
        obj = f"$(OBJDIR)/{unit.get('object')}"
        prereqs = " ".join(f"$(OBJDIR)/{o}" for o in (unit.get("prerequisite_objects") or []))
        prereqs = (prereqs + " ") if prereqs else ""
        unit_rules.append(
            f"{obj}: {src} {prereqs}| $(OBJDIR)\n"
            f"\t{cc} -c {src} -o {obj}")
    link_objs = " ".join(
        f"$(OBJDIR)/{o}" for o in (graph.get("link") or {}).get("objects") or [])
    rules_block = "\n\n".join(unit_rules)
    return f"""\
# Deterministic Makefile authored by the conductor from the CodegenBundle build graph
# (Z2 pure producer). Out-of-source capable: OBJDIR/BINDIR/RUNDIR default to "." and are
# overridden by Build (compile_project) and Validate.execute (run_quality_checks).
{_padded(rules['compiler_variable'])}:= {compiler}
OBJDIR  ?= .
BINDIR  ?= .
RUNDIR  ?= .
{_padded(rules['flags_variable'])}?= {rules['flags']}

BIN ?= {bin_name}
SPEC ?= spec.ir.yaml
CASES ?= {cases_default}

.PHONY: all test clean
.DEFAULT_GOAL := all

all: $(BINDIR)/$(BIN)

{rules_block}

$(BINDIR)/$(BIN): {link_objs} | $(BINDIR)
\t{cc} {link_objs} -o $(BINDIR)/$(BIN)

# $(sort ...) dedups the target list when OBJDIR==BINDIR (in-source make, both ".").
$(sort $(OBJDIR) $(BINDIR)):
\tmkdir -p $@

test:
\ttest -x $(BINDIR)/$(BIN) || {{ echo "error: $(BINDIR)/$(BIN) not built; run 'make all' first" >&2; exit 1; }}
\tmkdir -p $(RUNDIR)/raw/state_snapshots/initial
\tcd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)

clean:
{_clean_recipe(rules)}"""


def targets(makefile_path: Path) -> set[str]:
    """The target names a Makefile's rule lines declare (a best-effort line reader, for the
    `run_quality_checks` gate's "the preset's target exists" check)."""
    targets: set[str] = set()
    for raw_line in makefile_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or raw_line.startswith("\t"):
            continue
        if ":" not in raw_line:
            continue
        head, _ = raw_line.split(":", 1)
        if "=" in head:
            continue
        for token in head.split():
            token_l = token.strip().lower()
            if token_l:
                targets.add(token_l)
    return targets
