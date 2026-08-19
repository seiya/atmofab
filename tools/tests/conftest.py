"""Test-session hygiene for the isolated backend homes.

`_prepare_claude_workflow_home` (and its codex twin) create a private home under
`/tmp` per orchestration, and nothing removes it: in production that is deliberate,
because `--resume` reads the transcripts it holds, and relocating those homes to a
durable root with a retention policy is issue #64's.

A TEST RUN is different. Every fixture that drives `record_launch` for a
claude-shaped leaf makes one, so a full suite leaves a few hundred behind on a
shared tmpfs and they accumulate across runs.

OWNERSHIP IS TRACKED, NOT INFERRED. This wraps the two preparation functions and
removes exactly the homes THIS process created. Two weaker rules were tried and
both were wrong in the dangerous direction: a plain before/after set difference
deletes a home belonging to a workflow started while the suite runs, and excluding
homes named by this checkout's `orchestration_meta.json` still deletes one
belonging to a run in a DIFFERENT checkout, whose metadata is not visible here.

The failure mode of this version is a leaked directory, never a deleted one: a
call site that somehow bypasses the wrapper leaves its home behind, which is
untidy rather than destructive.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _remove_isolated_homes_this_session_created():
    import tools.orchestration_runtime as runtime

    created: set[Path] = set()
    originals = {}
    for name in ("_prepare_claude_workflow_home", "_prepare_codex_workflow_home"):
        original = getattr(runtime, name)
        originals[name] = original

        def _wrapper(*args, _original=original, **kwargs):
            isolation = _original(*args, **kwargs)
            home = isolation.get("home") if isinstance(isolation, dict) else None
            if isinstance(home, str) and home.strip():
                created.add(Path(home))
            return isolation

        setattr(runtime, name, _wrapper)
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(runtime, name, original)
        for path in created:
            shutil.rmtree(path, ignore_errors=True)
