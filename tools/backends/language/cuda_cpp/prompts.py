"""The CUDA C++ authoring and review rules the pure `generate` prompts carry (issue #289, R4-b PR-4).

The `prompt_fragments` capability, shaped like the Fortran backend's: the fragment files under
`tools/prompt_templates/backends/language/cuda_cpp/` hold what replaces a neutral template's
`{{language:<name>}}` markers for this language (format: `tools/prompt_fragments.py`).

Both shapes have fragments: the `harness` templates (`pure_generate_generate_harness.txt`,
`pure_generate_verify_harness.txt`, issue #289 R4-b PR-4) and the physics templates
(`pure_generate_generate.txt`, `pure_generate_verify.txt`, R4-b PR-6, written with the runner this
language renders). `fragments` raises for a template with no file, so a composer asked for one
anyway is refused rather than handed another language's rules.

Stdlib only, plus the neutral fragment parser.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from tools.prompt_fragments import parse_fragments

#: Where this language's fragment files live.
FRAGMENT_DIR = (Path(__file__).resolve().parents[3]
                / "prompt_templates" / "backends" / "language" / "cuda_cpp")


@cache
def fragments(template: str) -> dict[str, str]:
    """The fragments for the neutral template `template` (its file stem without `pure_`). A
    template this language has no fragment file for raises (module docstring)."""
    path = FRAGMENT_DIR / f"{template}.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"no cuda_cpp prompt fragments for {template!r}: {path} ({exc})") from exc
    return parse_fragments(text, source=str(path))


#: The CUDA C++ binding of the runner-output contract's JSON serialization rules, inlined after
#: `docs/workflow/RUNNER_OUTPUT_CONTRACT.md` wherever that document is inlined whole.
RUNNER_OUTPUT_DOCUMENT = (Path(__file__).resolve().parents[4]
                          / "docs" / "backends" / "language" / "cuda_cpp" / "RUNNER_OUTPUT.md")


def runner_output_document() -> str:
    """The runner-output binding, whole. Raises `OSError` / `UnicodeError` when it cannot be
    read; the caller turns that into a named fail-closed outcome."""
    return RUNNER_OUTPUT_DOCUMENT.read_text(encoding="utf-8")


#: The gate rule a certified exemplar most often predates, appended to the neutral exemplar
#: block's "where the exemplar and a contract disagree, the contract wins"
#: (`orchestration_runtime._build_exemplar`).
EXEMPLAR_GATE_DRIFT_NOTE = (
    "In particular, the `Generate.gate` lint check fails on EVERY compiler warning, so an "
    "exemplar that leaves an interface-fixed parameter unread, or declares a variable it never "
    "reads, does not pass it now: mark such a parameter read with `(void)name;` as the first "
    "statement of the body, and delete such a variable.")
