"""The `gfortran` compiler's `checked_flags` capability (issue #589 PR-0).

The flags a second, bounds-checked build of the certified sources is compiled with, appended to
the language's own flag string (`tools/backends/build_system/make/execute.py`
`checked_build_overrides`): a write outside an array's declared bounds then aborts the run with
the runtime's diagnostic, where the certified build lets it pass silently and the captures, which
read only the captured arrays, cannot see it (issue #588 §"Out-of-bounds writes the captures
cannot see"). `Validate.execute` runs that binary as the quality check's `checked` row
(`docs/workflow/phases/phase_04_validate.md` §4-2).

What it does not see is in `docs/backends/compiler/gfortran/TOOLCHAIN.md` §Checked build.

Stdlib only.
"""

from __future__ import annotations

#: Appended after the language's flags (`fortran/control_file.flags`), never in place of them:
#: those already carry the module-directory pair, and a second module-directory flag is fatal.
CHECKED_FLAGS: tuple[str, ...] = ("-fcheck=bounds",)
