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

from functools import lru_cache
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
        joined = "\n".join(body)
        out[name] = joined[:-1] if joined.endswith("\n") else joined
    return out


@lru_cache(maxsize=None)
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
