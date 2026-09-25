#!/usr/bin/env python3
"""The Fortran binding of the checks-module contract (issue #289, R4-b PR-2).

The `checks_abi` capability. `docs/workflow/CHECKS_MODULE_CONTRACT.md` is language-neutral: the
callbacks, the argument roles, what the bound state means, what the module must not do. This
module serves the document that says how each of those is spelled in Fortran, numbered section
for section like the neutral one (§1-§4), plus §5 — the legality and gate-guard rules every
leaf-authored Fortran source of a `Generate` node is held to. The conductor slices it with the
same numbered-section engine it slices the neutral contract with.

Stdlib only.
"""

from __future__ import annotations

from pathlib import Path

#: The binding document, under the placement `docs/BACKEND_BOUNDARY.md` gives a backend's docs.
DOCUMENT_PATH = (Path(__file__).resolve().parents[4]
                 / "docs" / "backends" / "language" / "fortran" / "CHECKS_ABI.md")


def document() -> str:
    """The whole binding document. Raises `OSError` / `UnicodeError` when it cannot be read;
    the caller turns that into a named fail-closed outcome."""
    return DOCUMENT_PATH.read_text(encoding="utf-8")
