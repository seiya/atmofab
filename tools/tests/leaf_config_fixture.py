"""Seed a synthetic repo root with the repository's committed leaf configuration.

Every test that drives `record_launch` (or the diagnostician profile) for the
`claude` backend needs `leaf_config/claude/settings.json` present, because
`_prepare_claude_workflow_home` validates and SHA-pins it before any launch and
fails closed when it is absent.

Driven by the REAL committed file rather than a hand-written copy: a fixture that
invented its own settings would keep passing after the committed file drifted,
which is the failure mode the leaf-config probe exists to catch in the first place.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LEAF_CONFIG_REL = Path("leaf_config") / "claude" / "settings.json"


def seed_claude_leaf_config(repo_root: Path) -> Path:
    """Copy this repository's committed leaf settings into `repo_root`."""
    destination = Path(repo_root) / LEAF_CONFIG_REL
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((REPO_ROOT / LEAF_CONFIG_REL).read_bytes())
    return destination
