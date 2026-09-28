#!/usr/bin/env python3
"""The MPI rules the pure `generate` prompts carry (issue #316, R4-c PR-4).

The `prompt_fragments` capability on the `parallel` axis. A neutral template marks the place a
parallel model's rules go with `{{parallel:<name>}}`, and the fragment files under
`tools/prompt_templates/backends/parallel/mpi/` hold what goes there for MPI: the Generate
presence floor this backend's `directives` module enforces, stated to the producer and scoped for
the reviewer. `tools/orchestration_runtime.py` composes them after the language's fragments. The
file format is neutral (`tools/prompt_fragments.py`).

Unlike a language's, this module has no runner-output binding: a parallel model's rules are ADDED
to a prompt, and only the template's markers ask for them.

Stdlib only, plus that neutral parser.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from tools.prompt_fragments import parse_fragments

#: Where this model's fragment files live — the placement `docs/BACKEND_BOUNDARY.md` gives a
#: backend's prompt templates.
FRAGMENT_DIR = (Path(__file__).resolve().parents[3]
                / "prompt_templates" / "backends" / "parallel" / "mpi")


@cache
def fragments(template: str) -> dict[str, str]:
    """The fragments for the neutral template `template` (its file stem without `pure_`, e.g.
    `generate_generate`). A template this model has no fragment file for raises: the composer
    only asks for a template whose text carries a `{{parallel:…}}` marker, so a missing file is
    a declaration that lied."""
    path = FRAGMENT_DIR / f"{template}.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"no mpi prompt fragments for {template!r}: {path} ({exc})") from exc
    return parse_fragments(text, source=str(path))
