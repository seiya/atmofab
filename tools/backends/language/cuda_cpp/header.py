"""The host-rendered published-surface header of a CUDA C++ node (issue #289, R4-b PR-4).

The `interface_header` capability. A CUDA C++ node is compiled one source file at a time and linked
(`docs/backends/language/cuda_cpp/BUNDLE_BINDING.md` §1), so every file that uses a node's
published surface needs its DECLARATIONS: the module parameters, the types, the prototypes and
the procedure declarations. The host renders them into `<spec_id>_model.cuh` from the node's IR
`public_api` — the certified transcription of `controlled_spec` §5.1 — with the same lowering the
§5.1 source pin applies (`signatures`). The leaf authors no declaration of the surface: its model
source includes this header and DEFINES the procedures, and a definition that disagrees with the
declaration is a different overload the §5.1 pin refuses, never a silent change of the ABI.

The header also carries the array view `atmofab::View<T, R>` the lowering names, guarded so that
several headers in one translation unit define it once.

Stdlib only, plus this package's `signatures`.
"""

from __future__ import annotations

from typing import Any

from tools.backends.language.cuda_cpp import signatures as cpp_signatures

#: The array view every rendered header defines (once per translation unit). Column-major: the
#: element at 0-based indices (i1, ..., iR) is
#: `data[i1 + extent[0] * (i2 + extent[1] * (... + extent[R-2] * iR))]`.
VIEW_DEFINITION = """\
#ifndef ATMOFAB_VIEW_DEFINED
#define ATMOFAB_VIEW_DEFINED
namespace atmofab {
// A rank-R, column-major, non-owning array view: `data` points at extent[0] * ... * extent[R-1]
// elements, the first index fastest.
template <class T, int R>
struct View {
    T* data;
    long extent[R];
};
}  // namespace atmofab
#endif
"""


def basename(spec_id: str) -> str:
    """The header's file name, beside the model source it declares."""
    return f"{spec_id}_model.cuh"


def _struct_from_public_api(public_api: dict[str, Any]) -> dict[str, Any]:
    """The neutral structured form (`tools/structured_signatures.py`) of an IR `public_api`:
    `signatures` entries split into types and procedures in their IR order, `interfaces` and
    `module_parameters` as they are. A malformed entry raises `SignatureParseError`."""
    types: list[Any] = []
    procedures: list[Any] = []
    for index, entry in enumerate(public_api.get("signatures") or []):
        signature = entry.get("signature") if isinstance(entry, dict) else None
        if not isinstance(signature, dict):
            raise cpp_signatures.SignatureParseError(
                f"public_api.signatures[{index}] carries no mapping `signature`")
        (types if "components" in signature else procedures).append(signature)
    interfaces = [entry.get("signature") if isinstance(entry, dict) else None
                  for entry in public_api.get("interfaces") or []]
    return {"module_parameters": list(public_api.get("module_parameters") or []),
            "types": types, "interfaces": interfaces, "procedures": procedures}


def render(spec_id: str, public_api: dict[str, Any]) -> str:
    """The whole header for node `spec_id`. Raises `SignatureParseError` when the IR's surface
    has no CUDA C++ lowering (the same refusal the §5.1 pin makes)."""
    declarations = cpp_signatures.render_signatures(_struct_from_public_api(public_api))
    body = "".join(f"{line}\n" if line else "\n" for line in declarations.splitlines())
    return (
        f"// {basename(spec_id)}: the published surface of {spec_id}, rendered by the host from\n"
        "// the node's IR public_api. Do not edit: define the procedures in the model source.\n"
        "#pragma once\n"
        "#include <string>\n"
        "#include <vector>\n"
        "\n"
        f"{VIEW_DEFINITION}"
        "\n"
        f"namespace {spec_id}_model {{\n"
        f"{body}"
        f"}}  // namespace {spec_id}_model\n"
    )
