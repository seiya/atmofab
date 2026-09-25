#!/usr/bin/env python3
"""The GNU-make sublanguage the deterministic Makefile gates read (issue #289, R4-b PR-3).

Moved unchanged out of `tools/validate_pipeline_semantics.py`: logical lines (`\\` continuations),
token normalization, `$(VAR)` / `${VAR}` expansion over the assignment operators, the rule
reader (basename view and the `$(OBJDIR)/`-prefix-aware view), the variable map, and the
per-target recipes. It is a reader for the Makefiles this repository's host authors and the
shapes a leaf-authored one takes, not a make implementation.

Stdlib only; imports nothing from the neutral core.
"""

from __future__ import annotations

import re
from pathlib import Path


def _makefile_logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    buffer = ""
    for raw_line in text.splitlines():
        if raw_line.startswith("\t"):
            continue

        line_no_comment = raw_line.split("#", 1)[0].rstrip()
        if not line_no_comment.strip():
            continue

        chunk = line_no_comment.strip()
        if chunk.endswith("\\"):
            buffer += chunk[:-1].strip() + " "
            continue

        logical = (buffer + chunk).strip()
        buffer = ""
        if logical:
            lines.append(logical)

    if buffer.strip():
        lines.append(buffer.strip())
    return lines



def _normalize_make_token(token: str) -> str | None:
    cleaned = token.strip().rstrip("\\")
    if not cleaned:
        return None
    if "%" in cleaned:
        return None

    cleaned = re.sub(r"\$\([^)]+\)", "", cleaned)
    cleaned = re.sub(r"\$\{[^}]+\}", "", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    if cleaned.startswith("$"):
        return None

    name = Path(cleaned).name.lower()
    if not name or "$" in name:
        return None
    return name



_MAKE_ASSIGNMENT_PATTERN = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)\s*([:+?]?)=\s*(.*)$"
)



_MAKE_VAR_REF_PATTERN = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")



def _expand_make_vars(
    expr: str,
    var_map: dict[str, str],
    depth: int = 8,
    strip_unknown: bool = False,
    preserve: set[str] | None = None,
) -> str:
    """Substitute ``$(NAME)`` / ``${NAME}`` for known names, bounded by depth.

    By default unknown variables are left intact so `_normalize_make_token`
    strips them, preserving behavior for genuinely unresolved references at
    rule-expansion time. With ``strip_unknown=True`` (used for immediate ``:=``
    expansion) any still-unresolved reference collapses to empty, matching GNU
    make: a ``:=`` RHS that forward-references a not-yet-defined variable
    expands to empty *now* and must not be resolved by a later definition. The
    depth bound stops self/cyclic references from looping forever.

    ``preserve`` names are never substituted nor stripped: their ``$(NAME)``
    reference survives verbatim. This is used by the directory-prefix-aware
    Makefile analysis to keep the ``$(OBJDIR)`` sentinel intact (so a
    ``$(MODEL_OBJ) := $(OBJDIR)/foo.o`` definition expands to
    ``$(OBJDIR)/foo.o`` rather than collapsing the out-of-source prefix away).
    """
    preserve = preserve or set()

    for _ in range(depth):
        if "$" not in expr:
            break

        def _sub(match: "re.Match[str]") -> str:
            name = match.group(1) or match.group(2)
            if name in preserve:
                return match.group(0)
            return var_map[name] if name in var_map else match.group(0)

        expanded = _MAKE_VAR_REF_PATTERN.sub(_sub, expr)
        if expanded == expr:
            break
        expr = expanded
    if strip_unknown:

        def _strip(match: "re.Match[str]") -> str:
            name = match.group(1) or match.group(2)
            return match.group(0) if name in preserve else ""

        expr = _MAKE_VAR_REF_PATTERN.sub(_strip, expr)
    return expr



def _parse_makefile_rules(makefile_text: str) -> dict[str, set[str]]:
    rules: dict[str, set[str]] = {}
    # Track simple variable definitions (`=` / `:=` / `?=` / `+=`) in file
    # order. GNU make expands a rule's targets and prerequisites immediately
    # when it reads the rule, so only definitions that appear *before* a rule
    # are visible to it; a forward reference expands to empty, which means the
    # prerequisite is genuinely absent (and `make -j` can race). Building the
    # map incrementally reproduces that: a not-yet-defined variable stays
    # unexpanded and `_normalize_make_token` drops it. `var_flavor` records
    # whether a variable is simply-expanded (`:=`) or recursively-expanded
    # (`=` / `?=`), which determines how `+=` treats the appended text.
    var_map: dict[str, str] = {}
    var_flavor: dict[str, str] = {}

    for line in _makefile_logical_lines(makefile_text):
        assign_match = _MAKE_ASSIGNMENT_PATTERN.match(line)
        if assign_match is not None:
            name, op, value = (
                assign_match.group(1),
                assign_match.group(2),
                assign_match.group(3).strip(),
            )
            if op == "?":
                # Conditional: only sets when undefined; defines a
                # recursively-expanded variable.
                if name not in var_map:
                    var_map[name] = value
                    var_flavor[name] = "recursive"
            elif op == ":":
                # Simply-expanded: RHS is expanded immediately at definition
                # time, so later redefinitions (or later first-definitions) of
                # referenced variables do not change this value. Unresolved
                # forward references collapse to empty, as make does now.
                var_map[name] = _expand_make_vars(
                    value, var_map, strip_unknown=True
                )
                var_flavor[name] = "simple"
            elif op == "+":
                if name not in var_map:
                    # No prior definition: `+=` acts like `=` (recursive).
                    var_map[name] = value
                    var_flavor[name] = "recursive"
                else:
                    # Appended text is expanded immediately for a
                    # simply-expanded variable, but kept raw (lazy) for a
                    # recursively-expanded one.
                    addition = (
                        _expand_make_vars(value, var_map, strip_unknown=True)
                        if var_flavor.get(name) == "simple"
                        else value
                    )
                    existing = var_map[name]
                    var_map[name] = (
                        (existing + " " + addition).strip() if existing else addition
                    )
            else:
                # Recursively-expanded (`=`): store raw, expand lazily at use.
                var_flavor[name] = "recursive"
                var_map[name] = value
            continue
        if ":" not in line:
            continue

        target_raw, prereq_raw = line.split(":", 1)
        target_raw = _expand_make_vars(target_raw, var_map)
        target_tokens = target_raw.split()
        if not target_tokens:
            continue

        prereq_expr = prereq_raw.split(";", 1)[0].replace("|", " ")
        prereq_expr = _expand_make_vars(prereq_expr, var_map)
        prereq_tokens = prereq_expr.split()
        prereqs = {
            norm
            for token in prereq_tokens
            if (norm := _normalize_make_token(token)) is not None
        }

        for target_token in target_tokens:
            target = _normalize_make_token(target_token)
            if target is None:
                continue
            rules.setdefault(target, set()).update(prereqs)
    return rules



def _apply_make_assignment(
    var_map: dict[str, str],
    var_flavor: dict[str, str],
    name: str,
    op: str,
    value: str,
    preserve: frozenset[str] | None = None,
) -> None:
    """Apply one variable assignment to ``var_map`` honoring make's flavors:
    `?=` (set if unset), `:=` (immediate expansion), `+=` (append, immediate for a
    simply-expanded var else lazy), `=` (recursive, stored raw). ``preserve`` names
    are kept as literal `$(NAME)` references through immediate expansion."""
    if op == "?":
        if name not in var_map:
            var_map[name] = value
            var_flavor[name] = "recursive"
    elif op == ":":
        var_map[name] = _expand_make_vars(
            value, var_map, strip_unknown=True, preserve=preserve
        )
        var_flavor[name] = "simple"
    elif op == "+":
        if name not in var_map:
            var_map[name] = value
            var_flavor[name] = "recursive"
        else:
            addition = (
                _expand_make_vars(value, var_map, strip_unknown=True, preserve=preserve)
                if var_flavor.get(name) == "simple"
                else value
            )
            existing = var_map[name]
            var_map[name] = (
                (existing + " " + addition).strip() if existing else addition
            )
    else:
        var_map[name] = value
        var_flavor[name] = "recursive"



# Make's built-in tool variables (`$(MAKE)`, `$(FC)`, …). They are predefined by
# make and usually never assigned in the Makefile, so they are absent from a map
# built only from explicit assignments. Preserving them through `:=`/`+=`
# expansion keeps the reference alive in an alias (`M := $(MAKE)` stores
# `$(MAKE)`, not the empty string), so a relink reached via that alias is still
# detected — and `$(MAKE)` matches `_RELINK_TOOL_PATTERN` literally.
_RELINK_BUILTIN_VARS = frozenset(
    {"MAKE", "FC", "CC", "CXX", "LD", "AR", "F90", "F95", "F77"}
)



def _makefile_full_var_map(makefile_text: str) -> dict[str, str]:
    """Variable map after reading the whole Makefile (definition order honored,
    `?=`/`:=`/`+=`/`=` flavors handled). Unlike the per-rule incremental map this
    resolves every reference (no preserved sentinels) so a variable-named target
    such as `$(BIN):` or `$(BINDIR)/$(BIN):` resolves to its concrete basename. The
    relink built-in tool variables are preserved through immediate expansion so an
    alias of `$(MAKE)`/`$(LD)`/… survives."""
    var_map: dict[str, str] = {}
    var_flavor: dict[str, str] = {}
    for line in _makefile_logical_lines(makefile_text):
        match = _MAKE_ASSIGNMENT_PATTERN.match(line)
        if match is None:
            continue
        _apply_make_assignment(
            var_map,
            var_flavor,
            match.group(1),
            match.group(2),
            match.group(3).strip(),
            preserve=_RELINK_BUILTIN_VARS,
        )
    return var_map



def _makefile_target_recipes(
    makefile_text: str, var_map: dict[str, str]
) -> dict[str, list[str]]:
    """Map of resolved target basename -> its recipe lines (tab-indented and
    inline `; …`). Target names are expanded with ``var_map`` so a variable-named
    rule (`$(BIN):`) is keyed by its concrete basename."""
    recipes: dict[str, list[str]] = {}
    current_targets: list[str] = []
    for raw_line in makefile_text.splitlines():
        if raw_line.startswith("\t"):
            for target in current_targets:
                recipes.setdefault(target, []).append(raw_line)
            continue
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#") or ":" not in raw_line:
            current_targets = []
            continue
        head, rest = raw_line.split(":", 1)
        if "=" in head:
            current_targets = []
            continue
        current_targets = [
            norm
            for tok in _expand_make_vars(head, var_map).split()
            if (norm := _normalize_make_token(tok)) is not None
        ]
        # An inline recipe (`target: prereqs ; recipe`) is part of the recipe and
        # must be classified too, not just tab-indented lines.
        inline_recipe = rest.partition(";")[2]
        if inline_recipe.strip():
            for target in current_targets:
                recipes.setdefault(target, []).append(inline_recipe)
    return recipes



# Out-of-source directory sentinels parameterized in generated Makefiles. They
# default to "." for in-source `make` but are overridden at Build/Validate time
# (e.g. `OBJDIR=<per-run tmp>`), so a prerequisite's `$(OBJDIR)/` prefix is NOT
# cosmetic: it determines which concrete target make resolves under an override.
_MAKE_DIR_SENTINELS = frozenset({"OBJDIR", "BINDIR", "RUNDIR"})



_OBJDIR_REF_PATTERN = re.compile(r"\$[({]OBJDIR[)}]")



def _token_has_objdir_prefix(token: str) -> bool:
    """True if a (sentinel-preserved) token references `$(OBJDIR)` / `${OBJDIR}`."""
    return bool(_OBJDIR_REF_PATTERN.search(token))



def _parse_makefile_rules_objdir_aware(
    makefile_text: str,
) -> tuple[dict[str, bool], dict[str, set[tuple[str, bool]]]]:
    """Parse rules while preserving the `$(OBJDIR)` sentinel so the out-of-source
    directory prefix survives basename normalization.

    Mirrors `_parse_makefile_rules`' incremental variable tracking but (1) never
    records the directory sentinels (`OBJDIR`/`BINDIR`/`RUNDIR`) as defined
    variables and (2) preserves their `$(...)` references through `:=`/`+=`
    immediate expansion. Returns:

    - ``target_has_objdir``: object-rule target basename → whether the producing
      rule writes it under `$(OBJDIR)/` (OR-ed across rules).
    - ``prereqs_diraware``: target basename → set of
      ``(prereq_basename, prereq_has_objdir_prefix)`` for its prerequisites.
    """
    var_map: dict[str, str] = {}
    var_flavor: dict[str, str] = {}
    target_has_objdir: dict[str, bool] = {}
    prereqs_diraware: dict[str, set[tuple[str, bool]]] = {}

    for line in _makefile_logical_lines(makefile_text):
        assign_match = _MAKE_ASSIGNMENT_PATTERN.match(line)
        if assign_match is not None:
            name, op, value = (
                assign_match.group(1),
                assign_match.group(2),
                assign_match.group(3).strip(),
            )
            # Never record the directory sentinels; their `$(...)` refs must stay
            # literal so a `$(OBJDIR)/`-prefixed value is structurally detectable.
            if name in _MAKE_DIR_SENTINELS:
                continue
            if op == "?":
                if name not in var_map:
                    var_map[name] = value
                    var_flavor[name] = "recursive"
            elif op == ":":
                var_map[name] = _expand_make_vars(
                    value, var_map, strip_unknown=True, preserve=_MAKE_DIR_SENTINELS
                )
                var_flavor[name] = "simple"
            elif op == "+":
                if name not in var_map:
                    var_map[name] = value
                    var_flavor[name] = "recursive"
                else:
                    addition = (
                        _expand_make_vars(
                            value,
                            var_map,
                            strip_unknown=True,
                            preserve=_MAKE_DIR_SENTINELS,
                        )
                        if var_flavor.get(name) == "simple"
                        else value
                    )
                    existing = var_map[name]
                    var_map[name] = (
                        (existing + " " + addition).strip() if existing else addition
                    )
            else:
                var_flavor[name] = "recursive"
                var_map[name] = value
            continue
        if ":" not in line:
            continue

        target_raw, prereq_raw = line.split(":", 1)
        target_raw = _expand_make_vars(target_raw, var_map, preserve=_MAKE_DIR_SENTINELS)
        target_tokens = target_raw.split()
        if not target_tokens:
            continue

        prereq_expr = prereq_raw.split(";", 1)[0].replace("|", " ")
        prereq_expr = _expand_make_vars(prereq_expr, var_map, preserve=_MAKE_DIR_SENTINELS)
        prereq_pairs: set[tuple[str, bool]] = set()
        for token in prereq_expr.split():
            norm = _normalize_make_token(token)
            if norm is None:
                continue
            prereq_pairs.add((norm, _token_has_objdir_prefix(token)))

        for target_token in target_tokens:
            target = _normalize_make_token(target_token)
            if target is None:
                continue
            target_has_objdir[target] = target_has_objdir.get(
                target, False
            ) or _token_has_objdir_prefix(target_token)
            prereqs_diraware.setdefault(target, set()).update(prereq_pairs)
    return target_has_objdir, prereqs_diraware
