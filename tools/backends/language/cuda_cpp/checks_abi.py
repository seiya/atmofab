"""The CUDA C++ binding of the checks-module contract (issue #289, R4-b PR-4).

The `checks_abi` capability: `docs/backends/language/cuda_cpp/CHECKS_ABI.md`, numbered section for
section like the neutral `docs/workflow/CHECKS_MODULE_CONTRACT.md` (§1-§4), plus §5 — the legality
and gate-guard rules every leaf-authored CUDA C++ source of a `Generate` node is held to, which the
`harness` producer is shown whole.

Stdlib only.
"""

from __future__ import annotations

from pathlib import Path

#: The binding document, under the placement `docs/BACKEND_BOUNDARY.md` gives a backend's docs.
DOCUMENT_PATH = (Path(__file__).resolve().parents[4]
                 / "docs" / "backends" / "language" / "cuda_cpp" / "CHECKS_ABI.md")


def document() -> str:
    """The whole binding document. Raises `OSError` / `UnicodeError` when it cannot be read;
    the caller turns that into a named fail-closed outcome."""
    return DOCUMENT_PATH.read_text(encoding="utf-8")
