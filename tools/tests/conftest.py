"""Test-session hygiene for the isolated backend homes.

`_prepare_claude_workflow_home` (and its codex twin) create a private home under
`/tmp` per orchestration, and nothing removes it: in production that is deliberate,
because `--resume` reads the transcripts it holds, and relocating those homes to a
durable root with a retention policy is issue #64's.

A TEST RUN is different. Every fixture that drives `record_launch` for a claude-shaped
leaf makes one, so a full suite leaves a few hundred behind on a shared tmpfs, and they
accumulate across runs. This removes the ones that appeared DURING this session and that no
orchestration in this checkout claims — snapshot before, difference after, minus
anything named by a `workspace/orchestrations/*/orchestration_meta.json`.

The metadata check is what makes the claim true: a set difference alone would also
contain a home created by a workflow run STARTED while the suite was running, and
deleting that takes its `settings.json` and every leaf transcript with it. A home a
real run owns is recorded before its first leaf launches, so it is excluded.
"""
from __future__ import annotations

import json
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


def _homes_claimed_by_an_orchestration() -> set[Path]:
    """Every isolated home a real run in this checkout has recorded."""
    claimed: set[Path] = set()
    root = Path(__file__).resolve().parents[2] / "workspace" / "orchestrations"
    try:
        metas = sorted(root.glob("*/orchestration_meta.json"))
    except OSError:
        return claimed
    for meta_path in metas:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        for key in ("claude_workflow_home", "codex_workflow_home"):
            raw = meta.get(key)
            if isinstance(raw, str) and raw.strip():
                claimed.add(Path(raw.strip()))
    return claimed


@pytest.fixture(scope="session", autouse=True)
def _remove_isolated_homes_this_session_created():
    before = _isolated_homes()
    yield
    for path in _isolated_homes() - before - _homes_claimed_by_an_orchestration():
        shutil.rmtree(path, ignore_errors=True)
