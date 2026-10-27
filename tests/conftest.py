#!/usr/bin/env python3
"""tests/conftest.py — 干净克隆的本地状态播种（2026-10-11）。

以下本地状态文件被 .gitignore 排除，但测试把它们当数据源/存在性前提：
  - `.claude/memory.md`（test_neighborhood 密度断言、test_graph_roundtrip ≥1 文件）
  - `.claude/settings.local.json`（test_dispatch_queue / test_llm_ergonomics 存在性）

文件缺失时播种最小合规 fixture；**存在时一律不动**（本机真实数据不受影响）。

注意：这里**不**设置 `XIANGXIONG_WORLD_ACTIVE`。phase0 金样测试会自建
workspace 并把 world 指向自己的 memory.md；全局绑定会让子进程读到项目根
的 seed memory，导致金样比对变成 `fixture.example.test` vs `phase0.example.test`。
"""

import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 干净克隆的最小世界 fixture：密度断言口径（test_neighborhood
# test_mechanism_is_not_degenerate）要求：
#   过半 edge 有锚点 · 邻域非空率 ≥ 0.5 · 至少一个邻域含 exclusion
# → 三条假设共享 /api/user/list 锚点，一条 counter 同锚点作 exclusion。
_SEED_MEMORY = """target: fixture.example.test
goal: 测试 fixture 世界（仅干净克隆播种用，非真实目标）
surface: /api/user/list 存在且返回 JSON
surface: /api/user/export 存在
hypothesis [observed]: 已知 /api/user/list → 无 Authorization GET → 期望 200 而非 401
hypothesis [observed]: 已知 /api/user/list → 带过期 token GET → 期望 401 而非 200
hypothesis [observed]: 已知 /api/user/export → 无 Authorization GET → 期望 200 而非 401
counter: /api/user/list 的批量枚举被限流封死，族级收口 (req_001)
"""


def _seed_local_state() -> None:
    claude_dir = os.path.join(PROJECT_ROOT, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    memory = os.path.join(claude_dir, "memory.md")
    if not os.path.exists(memory):
        with open(memory, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_SEED_MEMORY)

    settings_local = os.path.join(claude_dir, "settings.local.json")
    settings_example = os.path.join(claude_dir, "settings.example.json")
    if not os.path.exists(settings_local) and os.path.exists(settings_example):
        with open(settings_example, "r", encoding="utf-8") as handle:
            example = handle.read()
        with open(settings_local, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(example)


_seed_local_state()
