"""The CUDA C++ binding of the checks-module contract (issue #289, R4-b PR-4).

The `checks_abi` capability: `docs/backends/language/cuda_cpp/CHECKS_ABI.md`, numbered section for
section like the neutral `docs/workflow/CHECKS_MODULE_CONTRACT.md` (§1-§4), plus §5 — the legality
and gate-guard rules every leaf-authored CUDA C++ source of a `Generate` node is held to, which the
`harness` producer is shown whole.

It also holds the ABI's C++ declaration table (`CHECKS_PUBLIC_NAMES`, `CHECKS_ABI_PARAMS`), the one
place the binding of the neutral argument table is written in code: the runner renderer declares
the callbacks from it (`runner.render_checks_header`) and the checks gates compare a leaf's
definitions against it (`source.checks_module_abi_facts`).

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


#: The fixed ABI of the leaf-authored checks source (`docs/workflow/CHECKS_MODULE_CONTRACT.md` §1,
#: bound in CHECKS_ABI.md §1): the same five names every language's runner calls.
CHECKS_PUBLIC_NAMES: tuple[str, ...] = (
    "case_setup", "case_run", "get_time",
    "checks_compute", "metric_compute",
)

#: The declaration of each callback, as `(parameter type, parameter name)` pairs in call order.
#: The types are spelled as `declarations.normalize` reports them, so a definition's parameter
#: types compare against these directly and its parameter names are free. `logical` binds to
#: `bool`, `integer` to `int`, `float64` to `double`, an `in` string to `const std::string&`, and
#: every `out` argument is a non-const reference.
CHECKS_ABI_PARAMS: dict[str, tuple[tuple[str, str], ...]] = {
    "case_setup": (("const std::string&", "case_id"), ("bool&", "ok")),
    "case_run": (("const std::string&", "case_id"), ("int&", "steps"),
                 ("int&", "cells_updated"), ("bool&", "ok")),
    "get_time": (("double&", "t"),),
    "checks_compute": (("const std::string&", "case_id"), ("const std::string&", "check_id"),
                       ("std::string&", "status")),
    "metric_compute": (("const std::string&", "case_id"), ("const std::string&", "name"),
                       ("double&", "val"), ("bool&", "is_na"), ("std::string&", "reason_na"),
                       ("bool&", "found")),
}


def checks_header_basename(spec_id: str) -> str:
    """The host-rendered checks header's file name, beside the checks source it declares."""
    return f"{spec_id}_checks.cuh"
