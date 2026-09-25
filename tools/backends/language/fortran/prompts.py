#!/usr/bin/env python3
"""The Fortran authoring and review rules the pure `generate` prompts carry (issue #289, R4-b PR-2).

The `prompt_fragments` capability. The neutral templates (`tools/prompt_templates/pure_*.txt`)
state the contract every language shares and mark the places a language's rules go with
`{{language:<name>}}`; the fragment files under `tools/prompt_templates/backends/language/fortran/`
hold what goes there for Fortran — the lint and syntax idioms, the lowering of neutral signature
tokens, the checks-module binding. `tools/orchestration_runtime.py` composes the two before it
substitutes anything, so a composed template is exactly the text the leaf reads.

Stdlib only.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

#: Where this language's fragment files live — the placement `docs/BACKEND_BOUNDARY.md` gives a
#: backend's prompt templates.
FRAGMENT_DIR = (Path(__file__).resolve().parents[3]
                / "prompt_templates" / "backends" / "language" / "fortran")

_HEADER = "@@ "


def parse_fragments(text: str, *, source: str = "<fragments>") -> dict[str, str]:
    """The `@@ <name>` sections of a fragment file, each body VERBATIM without the newline that
    ends it. Lines before the first header must be `#` comments or blank; a repeated or empty
    name is refused, because the composer would otherwise pick one silently."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for number, line in enumerate(text.split("\n"), start=1):
        if line.startswith(_HEADER):
            name = line[len(_HEADER):].strip()
            if not name or name in sections:
                raise ValueError(f"{source}:{number}: empty or repeated fragment name {name!r}")
            current = sections[name] = []
        elif current is None:
            if line.strip() and not line.startswith("#"):
                raise ValueError(f"{source}:{number}: text before the first `@@` header")
        else:
            current.append(line)
    out: dict[str, str] = {}
    for name, body in sections.items():
        out[name] = "\n".join(body).removesuffix("\n")
    return out


@cache
def fragments(template: str) -> dict[str, str]:
    """The fragments for the neutral template `template` (its file stem without `pure_`, e.g.
    `generate_generate`). A template this language has no fragment file for raises: the
    composer only asks for a template whose text carries a marker, so a missing file is a
    declaration that lied."""
    path = FRAGMENT_DIR / f"{template}.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"no fortran prompt fragments for {template!r}: {path} ({exc})") from exc
    return parse_fragments(text, source=str(path))


#: The Fortran binding of the runner-output contract's JSON serialization rules, inlined after
#: `docs/workflow/RUNNER_OUTPUT_CONTRACT.md` wherever that document is inlined whole.
RUNNER_OUTPUT_DOCUMENT = (Path(__file__).resolve().parents[4]
                          / "docs" / "backends" / "language" / "fortran" / "RUNNER_OUTPUT.md")


def runner_output_document() -> str:
    """The runner-output binding, whole. Raises `OSError` / `UnicodeError` when it cannot be
    read; the caller turns that into a named fail-closed outcome."""
    return RUNNER_OUTPUT_DOCUMENT.read_text(encoding="utf-8")


#: The gate rule a certified exemplar most often predates, and how to satisfy it, appended to the
#: neutral exemplar block's "where the exemplar and a contract disagree, the contract wins"
#: (`orchestration_runtime._build_exemplar`). It names this language's compiler warning classes
#: and its binding idiom, so it is this backend's (issue #289, R4-b PR-4; byte-identical to the
#: text it replaces).
EXEMPLAR_GATE_DRIFT_NOTE = (
    "In particular, an exemplar certified before the `Generate.gate` gate promoted its "
    "current `-Werror` classes can show an ABI-fixed dummy "
    "(`name` / `case_id`) left unreferenced — that shape now fails the gate; bind it with "
    "`associate (unused_<name> => <name>); end associate` per §5 of the target "
    "language's checks-ABI binding (`docs/backends/language/<language>/CHECKS_ABI.md`).")
