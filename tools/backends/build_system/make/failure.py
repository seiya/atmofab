#!/usr/bin/env python3
"""Mechanical classification of a failed `make` build (phase_03_build.md; no LLM).

Moved unchanged out of `tools/workflow_conductor.py` (issue #289, R4-b PR-3). The categories are
the phase contract's neutral vocabulary; the patterns that pick one out of a failed build's
output are what make and the linkers it drives print.
"""

from __future__ import annotations


def classify_build_failure(return_code: int, stderr: str) -> str:
    """`make_error` (make found no rule for a target), `link_error` (the linker could not resolve
    a symbol), else `compile_error`."""
    s = (stderr or "").lower()
    if "no rule to make target" in s:
        return "make_error"
    if "undefined reference" in s or "unresolved external" in s:
        return "link_error"
    return "compile_error"
