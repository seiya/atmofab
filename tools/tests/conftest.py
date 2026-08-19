"""Test-session hygiene for the isolated backend homes.

`_prepare_claude_workflow_home` (and its codex twin) create a private home under
`/tmp` per orchestration, and nothing removes it: in production that is deliberate,
because `--resume` reads the transcripts it holds, and relocating those homes to a
durable root with a retention policy is issue #64's.

A TEST RUN is different. Every fixture that drives `record_launch` for a claude-shaped
leaf makes one, so a full suite leaves a few hundred behind on a shared tmpfs, and they
accumulate across runs. This removes exactly the ones THIS session created — snapshot
before, difference after — so a home belonging to a real run, or to a concurrently
running session, is never touched.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

_TMP = Path("/tmp")
_PREFIXES = ("metdsl-claude-", "metdsl-codex-")


def _isolated_homes() -> set[Path]:
    found: set[Path] = set()
    for prefix in _PREFIXES:
        try:
            found.update(p for p in _TMP.glob(f"{prefix}*") if p.is_dir())
        except OSError:
            continue
    return found


@pytest.fixture(scope="session", autouse=True)
def _remove_isolated_homes_this_session_created():
    before = _isolated_homes()
    yield
    for path in _isolated_homes() - before:
        shutil.rmtree(path, ignore_errors=True)
