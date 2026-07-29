#!/usr/bin/env python3
"""
state.py — 纯 JSON I/O，零业务逻辑。跨 session 状态持久化层。

原则：
- 只做文件读写，不判断、不推理、不格式化
- runtime.json 只保留 round/target（ARCHITECTURE.md §7: 没有模式）
"""

import json
import os

RUNTIME_PATH = os.path.join(os.path.dirname(__file__), "runtime.json")
MEMORY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".claude", "memory.md")


def load_runtime() -> dict:
    """读取 runtime.json。不存在时返回默认值。"""
    if not os.path.exists(RUNTIME_PATH):
        return {"round": 0, "target": ""}
    with open(RUNTIME_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_runtime(rt: dict):
    """写回 runtime.json。"""
    with open(RUNTIME_PATH, "w", encoding="utf-8") as f:
        json.dump(rt, f, ensure_ascii=False, indent=2)


def get_target(runtime: dict) -> str:
    """从 memory.md 读取当前目标（纯元数据读取，不分析内容）。"""
    if not os.path.exists(MEMORY_PATH):
        return ""
    with open(MEMORY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("target:"):
                return line.split(":", 1)[1].strip()
    return ""
