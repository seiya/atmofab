"""The file format of a language backend's prompt fragments (issue #289, R4-b PR-4).

A fragment file under `tools/prompt_templates/backends/language/<id>/` holds the text a neutral
`pure_*.txt` template's `{{language:<name>}}` markers are replaced with, as `@@ <name>` sections.
The FORMAT is every language's (`orchestration_runtime._compose_language_fragments` composes by
it), so its parser is neutral; what each section says is the backend's. It lived in the first
language backend's `prompts.py` until a second language needed the same parser.

Stdlib only.
"""

from __future__ import annotations

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
