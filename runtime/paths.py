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


def safe_name(identity: str) -> str:
    """纯转换（单一权威，无 IO、无回退、无身份解析）：已知身份串 → results 目录名。

    全仓 target→目录的**路径拼接**只准用这一个实现。2026-10-06 收敛前，同一裸转换
    有 8 处逐字副本（deepdive/diff_scan/execute/experiment/http_request/intents/
    protocol_scan/project._target_safe_name），本仓库已有四次副本漂移学费
    （EVIDENCE_BLOCK_RE / normalize_status / filelock / digest 双渲染）。

    注意语义分层：
    - 本函数回答「这个身份串拼成什么目录名」，目标目录可以尚不存在（写入路径拼接用）；
    - `merge.resolve_world_dir(body)` 回答「现存 results/ 里哪个目录是这个 target 的世界」，
      会读盘、过滤证据目录、确认首标签碰撞——两者不可互替。
    若输入是 target body（可能带 https:// 前缀/路径），先过 `merge.target_identity`。
    """
    return identity.replace(":", "_").replace("/", "_").replace("\\", "_")


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
