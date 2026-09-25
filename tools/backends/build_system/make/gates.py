#!/usr/bin/env python3
"""The deterministic gates over a node's `src/Makefile` (issue #289, R4-b PR-3).

Moved unchanged out of `tools/validate_pipeline_semantics.py`: the overridable-`BIN` rule, the
module-dependency / out-of-source prerequisite rule, the `make test` no-relink rule and the
`make test` case-invocation rule, with the shell-command reader the no-relink rule stands on.
The validator reaches them through `registry.capability_module("build_system", "make",
"control_file")` and decides only WHEN they apply; the language half of the prerequisite rule
(which sources exist and which modules each one uses) is asked of the language backend's
`source_reading` capability, which the validator hands in.

Stdlib only; imports nothing from the neutral core.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tools.backends.build_system.make.parse import (
    _MAKE_ASSIGNMENT_PATTERN,
    _apply_make_assignment,
    _expand_make_vars,
    _makefile_full_var_map,
    _makefile_logical_lines,
    _makefile_target_recipes,
    _normalize_make_token,
    _parse_makefile_rules,
    _parse_makefile_rules_objdir_aware,
)


def _makefile_relinking_recipe_targets(
    recipes: dict[str, list[str]], var_map: dict[str, str]
) -> set[str]:
    """Targets whose recipe relinks the binary — i.e. invokes a recursive make or
    a compiler/linker. A target that only runs the (already-built) binary, mkdirs,
    cleans up, etc. does not relink and is not included."""
    return {
        target
        for target, body in recipes.items()
        if any(_recipe_line_relinks(line, var_map) for line in body if line.strip())
    }



# BIN assignment forms. `?=` is overridable by the make environment (the only channel
# Validate.execute's run_quality_checks make_test has); `=`/`:=`/`+=` are not, so a
# make-env BIN override is silently ignored and `make test` desyncs from the binary Build
# produced with its command-line BIN override.
# Leading whitespace is spaces only: a make variable ASSIGNMENT cannot start with a tab
# (a tab-indented line is a recipe command, e.g. a shell `BIN=...` inside a target body),
# so excluding a leading tab avoids a false positive on recipe lines.
_MAKE_BIN_ASSIGN_RE = re.compile(r"^[ ]*BIN[ \t]*(\?=|:=|\+=|=)", re.MULTILINE)



_MAKE_BIN_REF_RE = re.compile(r"\$[({]BIN[)}]")



def validate_bin_overridable(
    makefile_path: Path, makefile_text: str, violations: list[str]
) -> None:
    """Require `BIN ?= <name>` when the Makefile builds a binary via `$(BIN)`.

    The VALUE is not constrained (the conductor imposes `<spec_id>_runner`); only the
    overridable `?=` form is required so the execute make_test environment override
    applies. A Makefile that never references `$(BIN)` (degenerate in-source object-only)
    is exempt.
    """
    ops = _MAKE_BIN_ASSIGN_RE.findall(makefile_text)
    references_bin = bool(_MAKE_BIN_REF_RE.search(makefile_text))
    if not ops and not references_bin:
        return
    has_overridable = any(op == "?=" for op in ops)
    has_hard = any(op != "?=" for op in ops)
    if has_hard or not has_overridable:
        violations.append(
            f"{makefile_path}: BIN must be declared overridable as `BIN ?= <name>` "
            "(not `=`/`:=`/`+=`) so Build and Validate.execute can impose the canonical "
            "<spec_id>_runner binary name (Validate.execute's make_test overrides BIN only "
            "via the environment, which applies to `?=` assignments only)"
        )



def validate_src_dir(
    src_dir: Path, violations: list[str], *, source_reading: Any, language: str
) -> None:
    """The module-dependency and out-of-source prerequisite rules over `src_dir/Makefile`.

    Which files are module sources, which modules each one uses, and what a compiled module's
    artifact is called are the node's LANGUAGE's (``source_reading``, its `source_reading`
    module: `MODULE_SOURCE_SUFFIXES`, `source_module_deps`, `MODULE_ARTIFACT_SUFFIX`); what a
    prerequisite is and where `$(OBJDIR)/` puts it are this build system's."""
    if not src_dir.is_dir():
        return

    module_suffixes = tuple(source_reading.MODULE_SOURCE_SUFFIXES)
    # `None`: the language leaves no module artifact beside an object (issue #289, R4-b PR-4),
    # so no prerequisite is ever taken for one.
    artifact_suffix = source_reading.MODULE_ARTIFACT_SUFFIX
    src_files = sorted(
        p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() in module_suffixes
    )
    if not src_files:
        return

    deps_by_stem = source_reading.source_module_deps(src_files)
    required_object_deps = {
        stem: deps for stem, deps in deps_by_stem.items() if deps
    }

    makefile_path = src_dir / "Makefile"
    if not makefile_path.exists():
        # The module-dependency build contract requires a Makefile; the
        # directory-prefix check below has nothing to inspect without one.
        if required_object_deps:
            violations.append(
                f"{makefile_path}: missing for {language} module dependency build"
            )
        return

    makefile_text = makefile_path.read_text(encoding="utf-8", errors="ignore")

    # The execution binary basename is NOT pinned to a specific VALUE here, but BIN must
    # be declared OVERRIDABLE (`BIN ?= <name>`). Build and Validate.execute impose the
    # canonical `<spec_id>_runner` binary name on the SAME Makefile so they always agree:
    # Build passes `BIN=...` on the make command line (overrides any assignment), but
    # Validate.execute re-runs `make test` via run_quality_checks, which can only pass BIN
    # through the environment — and a make environment value overrides a `?=` assignment
    # only (not a plain `=`/`:=`/`+=`). A hard BIN assignment would therefore desync
    # `make test`'s `$(BINDIR)/$(BIN)` guard from the binary Build actually produced. The
    # default VALUE stays the generator's choice (any value; conductor overrides it), so
    # this is a structural `?=` requirement, not the removed `BIN must be <spec_id>_runner`
    # value gate. Mirrors the `OBJDIR/BINDIR/RUNDIR ?=` out-of-source parameterization.
    validate_bin_overridable(makefile_path, makefile_text, violations)

    rules = _parse_makefile_rules(makefile_text)
    # Directory-prefix-aware view: detects a prerequisite whose `$(OBJDIR)/`
    # prefix structure disagrees with its producing object rule. The basename
    # `rules` view above normalizes the prefix away, so a bare `foo.o`
    # prerequisite passes there even though the only rule that produces it
    # targets `$(OBJDIR)/foo.o` — which breaks `make -j` under an out-of-source
    # OBJDIR override (no rule makes the bare target). See SKILL.md L42.
    target_has_objdir, prereqs_diraware = _parse_makefile_rules_objdir_aware(
        makefile_text
    )
    # Basename-level: every used-module dependency must be a prerequisite of
    # the consuming object rule (the `.mod` or the `.o`). Gated by
    # `required_object_deps` — only meaningful when sources have local `use`
    # dependencies on each other.
    for stem, deps in sorted(required_object_deps.items()):
        object_target = f"{stem}.o"
        prereqs = rules.get(object_target)
        if prereqs is None:
            violations.append(
                f"{makefile_path}: missing explicit object dependency rule ({object_target})"
            )
            continue

        for dep_stem in sorted(deps):
            dep_obj = f"{dep_stem}.o"
            if artifact_suffix is None:
                if dep_obj not in prereqs:
                    violations.append(
                        f"{makefile_path}: {object_target} missing prerequisite for used module "
                        f"({dep_obj})")
                continue
            dep_mod = f"{dep_stem}{artifact_suffix}"
            if dep_mod not in prereqs and dep_obj not in prereqs:
                violations.append(
                    f"{makefile_path}: {object_target} missing prerequisite for used module ({dep_mod} or {dep_obj})"
                )

    # Out-of-source correctness (directory-prefix consistency) runs
    # UNCONDITIONALLY — independent of `required_object_deps` and source count.
    # A bare object prerequisite on a link rule (e.g. `$(BINDIR)/app: main.o`
    # against `$(OBJDIR)/main.o:`) breaks the out-of-source Build even when no
    # source has a local `use` dependency, so this pass must not be gated by the
    # module-dependency early returns above.
    # Checked across
    # ALL rules — object rules AND the link/default rule. When an object (or its
    # paired `.mod`) is produced under `$(OBJDIR)/`, every rule that consumes it
    # must reference it with the same `$(OBJDIR)/` prefix. A bare basename
    # prerequisite has no producing rule once OBJDIR is overridden, so
    # `make -j` aborts with "No rule to make target" — the same prefix mismatch
    # whether the bare name appears on the runner object rule or the link rule.
    def _produced_under_objdir(prereq_basename: str) -> bool:
        # The object itself is produced under $(OBJDIR)/...
        if target_has_objdir.get(prereq_basename, False):
            return True
        # ...or it is a `.mod` whose sibling `.o` is produced under $(OBJDIR)/
        # (the .mod is typically a by-product of compiling that .o and may have
        # no explicit rule of its own).
        if artifact_suffix is not None and prereq_basename.endswith(artifact_suffix):
            sibling_obj = f"{prereq_basename[: -len(artifact_suffix)]}.o"
            return target_has_objdir.get(sibling_obj, False)
        return False

    for consumer_target in sorted(prereqs_diraware):
        bare = sorted(
            {
                prereq_basename
                for prereq_basename, has_objdir in prereqs_diraware[consumer_target]
                if not has_objdir and _produced_under_objdir(prereq_basename)
            }
        )
        if bare:
            violations.append(
                f"{makefile_path}: {consumer_target} prerequisite "
                f"({', '.join(bare)}) must carry the same $(OBJDIR)/ prefix as its "
                f"producing rule target; a bare basename has no rule under an "
                f"out-of-source OBJDIR override and breaks make -j (no rule to make target)"
            )



# A relinking command word: a recursive make or a compiler/linker/archiver, as
# the *first* word of a shell command. Anchored at the start of an extracted
# command word, so a tool name appearing inside an argument (e.g. an echo message)
# is never matched. The make-variable form allows an optional second `$` so a
# recipe-escaped `$$(MAKE)` / `$${MAKE}` is recognized too. `g++`/`c++`/`clang++`
# need no trailing word boundary (a `+` is not a word char). The command word's
# path basename is also tested so an absolute path (`/usr/bin/make`) is recognized.
# Build drivers (cmake/ninja/meson/libtool) are intentionally NOT matched: in a
# `build_system=make` Makefile they appear mostly in non-building utility modes
# (`cmake -E`, `ninja -t`, `meson test`, `libtool --mode=execute`), so a bare
# command-word match would be a false positive.
_RELINK_TOOL_PATTERN = re.compile(
    r"""^(?:
        \$\$?[({](?:MAKE|FC|CC|CXX|NVCC|LD|AR|F90|F95|F77)[)}]
      | (?:make|gmake|mingw32-make|gfortran|gcc|clang|cc|ld|ar|nvcc|nvfortran|ifort|ifx|f90|f95|f77)\b
      | (?:g|c|clang)\+\+
    )""",
    re.VERBOSE,
)



# The phony test entrypoints are not themselves build targets, so a `check: test`
# alias (canonical) must not be read as a relink-triggering prerequisite.
_PHONY_TEST_TARGETS = frozenset({"test", "check"})



# Shell control words that introduce another command directly (no separator), so
# the *following* token is itself a command word (e.g. `then $(MAKE)`, `if ! make`).
_SHELL_CMD_PREFIX_KEYWORDS = frozenset(
    {"if", "elif", "then", "else", "while", "until", "do", "time", "!"}
)



# Command wrappers whose *next* token is the wrapped command (`ccache gfortran …`,
# `env FC=gfortran make …`): skip the wrapper and examine that command word. Only
# wrappers that take no argument before the command are included — wrappers that
# take their own options/args first (`timeout 60 make`, `nice -n10 make`,
# `sudo -u x make`, `xargs -n1 make`) would mis-identify the arg as the command,
# so they are intentionally omitted (a documented low-realism gap).
_SHELL_CMD_WRAPPER_PREFIXES = frozenset(
    {"ccache", "distcc", "sccache", "nohup", "env"}
)



# A leading `NAME=value` shell assignment precedes the actual command, so the
# following token is the command word (e.g. `FC=gfortran make …`).
_SHELL_ASSIGNMENT_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")



def _shell_command_words(recipe: str) -> list[str]:
    """Extract the command word (first token) of each shell command in a recipe
    line, honoring quotes and make's `$(...)`/`${...}` syntax. Commands are
    delimited by unquoted `;` / `&` / `|`, and by `{` / `(` *group openers* (a `{`
    or `(` not immediately following `$`, so `${VAR}` / `$(VAR)` stay intact).
    Surrounding quotes are stripped from a quoted command word (`"$(MAKE)"` ->
    `$(MAKE)`), while separators/words inside an argument's quotes are ignored."""
    words: list[str] = []
    word: list[str] = []
    reading = False
    cmd_start = True
    quote: str | None = None
    prev = ""

    def emit() -> None:
        nonlocal reading, word
        if reading:
            words.append("".join(word))
            word = []
            reading = False

    for char in recipe:
        if quote is not None:
            if char == quote:
                quote = None
            elif reading:
                word.append(char)
            prev = char
            continue
        if char in "'\"":
            if cmd_start:
                reading = True  # a quoted command word; the quotes are dropped
            quote = char
            prev = char
            continue
        if char == "#" and (prev == "" or prev in " \t;&|({"):
            # An unquoted `#` at a word boundary starts a shell comment; the rest
            # of the line is not executed (`… $(BIN)  # build first; make all`).
            break
        if char in ";&|" or (char in "{(" and prev != "$"):
            emit()
            cmd_start = True
            prev = char
            continue
        if char in " \t":
            if reading:
                emit()
                # A shell control keyword (`then`/`do`/`!`/…), a command wrapper
                # (`ccache`/`env`/…), or a leading `NAME=value` assignment is
                # followed by another command, so the next token is also a command
                # word.
                cmd_start = (
                    words[-1] in _SHELL_CMD_PREFIX_KEYWORDS
                    or words[-1] in _SHELL_CMD_WRAPPER_PREFIXES
                    or bool(_SHELL_ASSIGNMENT_PREFIX.match(words[-1]))
                )
            prev = char
            continue
        # Ordinary char.
        if cmd_start and not reading and char in "@+-":
            prev = char  # skip leading make recipe prefixes (@ silent, - ignore, + force)
            continue
        if cmd_start:
            reading = True
            word.append(char)
        prev = char
    emit()
    return words



_SHELL_DASH_C_ARG = re.compile(
    r"""\b(?:sh|bash|dash|zsh|ksh)\s+-[A-Za-z]*c\s+   # -c, possibly with combined flags (-lc)
        ("(?:[^"]*)"|'(?:[^']*)'|\S+)""",
    re.VERBOSE,
)



_BACKTICK_SPAN = re.compile(r"`([^`]*)`")



def _command_word_relinks(command: str, var_map: dict[str, str] | None) -> bool:
    """True if a command word is a relink tool, after optionally resolving a
    make-variable alias (`$(LINK)` -> `gfortran`) via ``var_map``."""
    candidates = {command}
    if var_map:
        candidates.add(_expand_make_vars(command, var_map))
    for candidate in candidates:
        if _RELINK_TOOL_PATTERN.search(candidate) or _RELINK_TOOL_PATTERN.search(
            candidate.rsplit("/", 1)[-1]
        ):
            return True
    return False



def _nested_command_texts(text: str) -> list[str]:
    """Shell-command substrings nested inside a recipe line: make `$(shell …)`
    function bodies, shell `$$(…)` command substitutions, backtick substitutions,
    and the argument of `sh -c` / `bash -c`. Returned so the relink scan can
    recurse into them (a relink hidden in `$(shell $(MAKE) …)` etc.)."""
    bodies: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] == "$" and i + 1 < n:
            j = i + 1
            shell_subst = False
            if text[j] == "$":  # `$$(` -> shell command substitution
                j += 1
                shell_subst = True
            if j < n and text[j] == "(":
                depth = 1
                k = j + 1
                start = k
                while k < n and depth:
                    if text[k] == "(":
                        depth += 1
                    elif text[k] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                body = text[start:k]
                if shell_subst:
                    bodies.append(body)
                else:
                    shell_fn = re.match(r"shell\s+(.*)", body, re.S)
                    if shell_fn:
                        bodies.append(shell_fn.group(1))
                i = k + 1
                continue
        i += 1
    bodies.extend(m.group(1) for m in _BACKTICK_SPAN.finditer(text))
    for match in _SHELL_DASH_C_ARG.finditer(text):
        arg = match.group(1)
        if len(arg) >= 2 and arg[0] in "'\"" and arg[-1] == arg[0]:
            arg = arg[1:-1]
        bodies.append(arg)
    return bodies



def _text_relinks(text: str, var_map: dict[str, str] | None, depth: int = 0) -> bool:
    if depth > 6:  # bound pathological nesting
        return False
    if any(_command_word_relinks(cmd, var_map) for cmd in _shell_command_words(text)):
        return True
    return any(
        _text_relinks(body, var_map, depth + 1) for body in _nested_command_texts(text)
    )



def _recipe_line_relinks(recipe_line: str, var_map: dict[str, str] | None = None) -> bool:
    """True if a recipe line executes a relinking command (recursive make, a build
    driver, or a compiler/linker) as a command word — at the top level or nested
    in a `$(shell …)` / `$$(…)` / backtick substitution or a `sh -c` body. Command
    words are expanded with ``var_map`` so a make-variable alias resolves first."""
    return _text_relinks(recipe_line.lstrip("\t"), var_map)



def validate_test_no_relink(
    src_dir: Path,
    violations: list[str],
    *,
    applies: bool,
) -> None:
    # The non-relinking `test`/`check` contract applies only to the make-based
    # quality-check toolchains (`Validate.execute` runs `make_test`/`make_check`
    # only then). Skip any other toolchain — a Makefile kept for local
    # convenience must not fail post_generate/post_build here.
    if not applies:
        return
    if not src_dir.is_dir():
        return
    makefile_path = src_dir / "Makefile"
    if not makefile_path.exists():
        return

    text = makefile_path.read_text(encoding="utf-8", errors="ignore")
    # Whole-file variable map: resolves variable-named targets/recipes and the
    # binary basename (`$(BIN)`). Targets/recipes are position-independent, so the
    # final map is correct for them; `test`/`check` *prerequisites* are resolved
    # with an incremental map below to honor make's read-time expansion order.
    full_var_map = _makefile_full_var_map(text)
    binary_basename = _normalize_make_token(_expand_make_vars("$(BIN)", full_var_map))
    recipes = _makefile_target_recipes(text, full_var_map)
    relinking_recipe_targets = _makefile_relinking_recipe_targets(recipes, full_var_map)

    # Incremental pass mirroring GNU make: a rule's prerequisites are expanded
    # immediately at read time, so only definitions seen *before* the rule are
    # visible (a forward reference expands to empty). Records each rule target's
    # resolved prerequisite basenames; `test`/`check` rules are captured for the
    # verdict.
    inc_var_map: dict[str, str] = {}
    inc_var_flavor: dict[str, str] = {}
    prereq_names_by_target: dict[str, set[str]] = {}
    test_check_rules: list[tuple[frozenset[str], set[str], str]] = []
    for line in _makefile_logical_lines(text):
        assign_match = _MAKE_ASSIGNMENT_PATTERN.match(line)
        if assign_match is not None:
            _apply_make_assignment(
                inc_var_map,
                inc_var_flavor,
                assign_match.group(1),
                assign_match.group(2),
                assign_match.group(3).strip(),
            )
            continue

        if ":" not in line:
            continue
        head, rest = line.split(":", 1)
        target_names = {
            norm
            for tok in _expand_make_vars(head, inc_var_map).split()
            if (norm := _normalize_make_token(tok)) is not None
        }
        if not target_names:
            continue

        # Right-hand side: prerequisites (normal + order-only, both built by make)
        # and an optional inline recipe (`target: prereqs ; recipe`).
        prereq_part, _, inline_recipe = rest.partition(";")
        prereq_names = {
            norm
            for tok in _expand_make_vars(
                prereq_part.replace("|", " "), inc_var_map
            ).split()
            if (norm := _normalize_make_token(tok)) is not None
        }
        for target in target_names:
            prereq_names_by_target.setdefault(target, set()).update(prereq_names)

        guarded_targets = target_names & _PHONY_TEST_TARGETS
        if guarded_targets:
            test_check_rules.append(
                (frozenset(guarded_targets), prereq_names, inline_recipe)
            )

    # A rule target relinks when made if its recipe builds (links) or it is the
    # binary itself, plus any target that transitively depends on such a target.
    # Computed as a fixpoint over the prerequisite graph; the phony test
    # entrypoints are excluded (a `check: test` alias is not a build prerequisite).
    relinking = set(relinking_recipe_targets)
    if binary_basename is not None:
        relinking.add(binary_basename)
    relinking -= _PHONY_TEST_TARGETS
    changed = True
    while changed:
        changed = False
        for target in set(prereq_names_by_target) - relinking - _PHONY_TEST_TARGETS:
            if prereq_names_by_target[target] & relinking:
                relinking.add(target)
                changed = True

    for guarded_targets, prereq_names, inline_recipe in test_check_rules:
        target_label = "/".join(sorted(guarded_targets))

        # Prerequisite relink: a prerequisite (normal or order-only) that is the
        # binary or a target that relinks when built.
        if prereq_names & relinking:
            violations.append(
                f"{makefile_path}: {target_label} target has a build prerequisite "
                f"that relinks the binary; the target must reference the existing "
                f"binary via a non-relinking recipe guard "
                f"'test -x $(BINDIR)/$(BIN) || {{ echo \"error: ...\" >&2; exit 1; }}' "
                f"with no build prerequisite, so Validate.execute does not write into "
                f"the read-only-bound binary/ (EROFS -> the phase fails)"
            )

        # Recipe relink: an inline (`; ...`) or tab-indented recipe line that
        # rebuilds the binary (recursive make or a compiler/linker invocation).
        recipe_lines: list[str] = []
        if inline_recipe.strip():
            recipe_lines.append(inline_recipe)
        for tgt in guarded_targets:
            recipe_lines.extend(recipes.get(tgt, []))
        for recipe_line in recipe_lines:
            if _recipe_line_relinks(recipe_line, full_var_map):
                violations.append(
                    f"{makefile_path}: {target_label} target recipe relinks the binary "
                    f"(recursive make or compiler/linker invocation); use a non-relinking "
                    f"fail-closed guard "
                    f"'test -x $(BINDIR)/$(BIN) || {{ echo \"error: ...\" >&2; exit 1; }}' "
                    f"so Validate.execute does not write into the read-only-bound "
                    f"binary/ (EROFS -> the phase fails)"
                )
                break



def validate_test_invokes_cases(
    src_dir: Path,
    violations: list[str],
    *,
    applies: bool,
) -> None:
    """Flag a ``test``/``check`` target whose recipe runs the runner binary but
    does NOT forward ``--cases $(SPEC) $(CASES)``.

    ``Validate.execute`` runs the binary two ways and compares them for value
    equality (``quality_check.json``): ``run_program`` invokes it as
    ``--cases <spec.ir.yaml> <case_id>...`` and ``make test`` must invoke it the
    same way. The conductor injects ``SPEC``/``CASES`` via the make-test env so
    the canonical recipe ``$(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)`` is
    byte-identical to ``run_program``. Two recipes desync the two invocations and
    are flagged: (a) a bare run (no ``--cases``) — the runner aborts, the
    candidate emits no ``diagnostics.json`` (``verdict_available=false``); and
    (b) a run that hardcodes ``--cases <spec> <ids>`` instead of referencing the
    ``$(SPEC)``/``$(CASES)`` variables — the env override has no effect and make
    test runs a different spec/case set than ``run_program`` (wrong-evidence
    comparison). The conductor-authored Makefile already satisfies this; the check
    guards a control file a LEAF would author for a compiled language the conductor
    writes none for (none is registered today). Best-effort static parse —
    the runtime ``quality_check`` is the deterministic backstop. Scoped to the
    make-based quality-check toolchains (same as the no-relink check)."""
    if not applies:
        return
    if not src_dir.is_dir():
        return
    makefile_path = src_dir / "Makefile"
    if not makefile_path.exists():
        return

    text = makefile_path.read_text(encoding="utf-8", errors="ignore")
    full_var_map = _makefile_full_var_map(text)
    recipes = _makefile_target_recipes(text, full_var_map)
    binary_basename = _normalize_make_token(_expand_make_vars("$(BIN)", full_var_map))

    # `make test` also runs the recipes of `test`/`check`'s prerequisite targets, so
    # a recipe that delegates the run to a helper (`test: run-qc`, run in `run-qc`)
    # must be traced. Build/relink targets (the binary itself + any compile/link
    # recipe) are EXCLUDED from the trace so a `$(FC) … -o $(BINDIR)/$(BIN)` line is
    # not misread as a runner invocation (their no-build-prerequisite contract is the
    # separate `validate_test_no_relink` gate's concern).
    rules = _parse_makefile_rules(text)
    build_targets = set(_makefile_relinking_recipe_targets(recipes, full_var_map))
    if binary_basename:
        build_targets.add(binary_basename)

    def _run_recipe_lines(entrypoint: str) -> list[str]:
        seen: set[str] = set()
        stack = [entrypoint]
        collected: list[str] = []
        while stack:
            t = stack.pop()
            if t in seen or t in build_targets:
                continue
            seen.add(t)
            collected.extend(recipes.get(t, []))
            stack.extend(rules.get(t, ()))
        return collected

    def _logical_recipe_lines(lines: list[str]) -> list[str]:
        # Fold trailing-`\` continuations so an invocation wrapped across physical
        # lines (`… $(BIN) \` / `  --cases …`) is scanned as one logical command.
        logical: list[str] = []
        buf = ""
        for raw in lines:
            chunk = raw.lstrip("\t")
            if chunk.rstrip().endswith("\\"):
                buf += chunk.rstrip()[:-1] + " "
                continue
            logical.append((buf + chunk).strip())
            buf = ""
        if buf.strip():
            logical.append(buf.strip())
        return logical

    def _segment_is_noise(seg: str) -> bool:
        # A segment that does not RUN the binary: the `test -x`/`[ -x ]` existence
        # guard or an `echo`/`printf` message (which may mention `$(BIN)` in its text,
        # e.g. the fail-closed guard's error string). Make recipe prefixes (`@`/`-`/
        # `+`) and a leading `{` (from `|| { echo … }`) are trimmed first.
        s = seg.strip().lower().lstrip("@-+{ \t")
        return (s.startswith("test ") or s.startswith("test\t") or s.startswith("[")
                or s.startswith("echo ") or s.startswith("echo\t") or s == "echo"
                or s.startswith("printf"))

    # Expand make variables (so a runner aliased via `RUNNER = $(BINDIR)/$(BIN)` is
    # still detected as a run) but PRESERVE `SPEC`/`CASES`: their `$(SPEC)`/`$(CASES)`
    # references must survive verbatim so the compliance check can confirm the recipe
    # forwards the env-injected values rather than hardcoding a spec/case list.
    for tgt in _PHONY_TEST_TARGETS:
        if tgt not in recipes and tgt not in rules:
            continue
        recipe_lines = _run_recipe_lines(tgt)
        if not recipe_lines:
            continue
        runs_binary = False
        noncompliant_run = False
        for line in _logical_recipe_lines(recipe_lines):
            expanded = _expand_make_vars(
                line, full_var_map, preserve={"SPEC", "CASES"})
            # Remove quote CHARACTERS (keep the content) so a shell-quoted forward
            # `--cases "$(SPEC)" "$(CASES)"` still exposes the `$(SPEC)`/`$(CASES)`
            # tokens, while a `;`/`|` inside a (now-unquoted) echo message that splits
            # a segment is harmless because echo segments are classified as noise.
            cleaned = expanded.replace('"', "").replace("'", "").replace("`", "").lower()
            # Split into shell command segments; compliance is checked on the SEGMENT
            # that runs the binary (not the whole line) so an echo mentioning `--cases`
            # elsewhere does not mask a bare run.
            for seg in re.split(r"&&|\|\||;|\|", cleaned):
                invokes = "$(bin)" in seg or "$(bindir)" in seg
                if not invokes and binary_basename:
                    invokes = re.search(
                        rf"\b{re.escape(binary_basename)}\b", seg) is not None
                if not invokes or _segment_is_noise(seg):
                    continue
                runs_binary = True
                # Compliant iff the run forwards the env-injected SPEC/CASES vars; a
                # bare run (no `--cases`) OR a hardcoded `--cases spec.ir.yaml c_old`
                # that ignores the env override both desync make test from run_program.
                forwards_spec = "$(spec)" in seg or "${spec}" in seg
                forwards_cases = "$(cases)" in seg or "${cases}" in seg
                if not ("--cases" in seg and forwards_spec and forwards_cases):
                    noncompliant_run = True
        if runs_binary and noncompliant_run:
            violations.append(
                f"{makefile_path}: {tgt} target does not invoke the runner as "
                "`$(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)` — the recipe must forward "
                "the `$(SPEC)`/`$(CASES)` make variables (a bare run, or a hardcoded "
                "`--cases <spec> <ids>` that ignores them, desyncs make test from "
                "run_program: the runner requires `--cases`, and Validate.execute "
                "injects the authoritative SPEC/CASES via the env, which override the "
                "`?=` defaults kept for local use) "
                "(docs/workflow/RUNNER_OUTPUT_CONTRACT.md §5 / phase_04_validate.md §4-1)"
            )
