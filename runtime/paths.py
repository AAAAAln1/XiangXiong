#!/usr/bin/env python3
"""
paths.py — Phase 1.1 path ownership only (H0–H15).

Knows where the active world file lives. Does not interpret world content.
Default physical path remains .claude/memory.md (no directory migration).
"""

from __future__ import annotations

import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ENV_WORLD_ACTIVE = "XIANGXIONG_WORLD_ACTIVE"


def project_root() -> str:
    """Repository / workspace root (parent of runtime/)."""
    return _PROJECT_ROOT


def world_active() -> str:
    """
    Absolute path to the active world memory file.

    - Default: <project_root>/.claude/memory.md
    - Override: env XIANGXIONG_WORLD_ACTIVE (absolute or relative to cwd)
    - No .xx migration; no schema; no belief logic.
    """
    override = os.environ.get(_ENV_WORLD_ACTIVE, "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(_PROJECT_ROOT, ".claude", "memory.md")
