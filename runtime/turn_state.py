#!/usr/bin/env python3
"""turn_state.py — 轮次机械状态（最近证据编号提示 + 轮次遥测）。

## 为什么有这个模块

merge 的证据闸 REJECT 后，LLM 的主要返工动作是开一个 Bash 去翻
「我刚发的请求编号是多少 / 最近的 art 是几」（实测：近半会话撞闸，
同一条 merge 命令重跑最高 30 次）。编号在工具回显里本来就有，纯机械可查。

http_request / execute / experiment 在成功产出证据时，把「最近编号 + UTC 时间戳」
记进本模块；merge REJECT 时读出来附在回执里（见 merge._evidence_hint）。

## 红线（F0 三重机械约束，缺一不可）

1. 只记录「最近产生」的编号——来源限定为三个工具的成功写入事件，
   **禁止按 claim 文本做任何匹配**；
2. 只在 REJECT 回执出现——不进投影、不进账本、不进成功路径输出；
3. 调用方回执必须带非背书措辞：「仅编号存在提示，不代表支持本结论」。

## 边界

- 这不是世界模型：不判断编号与结论的关系、不进投影；
- 30 分钟新鲜窗（FRESH_SECONDS），过期视为不存在；
- 文件受 memory_guard 保护；写入只发生在 runtime 工具内部（hook 不检工具内部写）；
- 所有公共函数**永不抛异常**（记录失败不得打断请求/执行主路径）。

schema v2（v1 只有 {"version":1,"turns_without_tool_use":N}，读入时平滑升级）：

    {
      "version": 2,
      "turns_without_tool_use": 0,
      "evidence": {
        "<safe_name(target)>": {
          "req": {"ref": "req_142", "ts": "2026-10-07T08:30:11+00:00"},
          "art": {"ref": "art-005", "ts": "..."},
          "exp": {"ref": "exp-008", "ts": "..."}
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

try:
    from .filelock import locked
    from .paths import project_root, safe_name
except ImportError:  # scripts/ 与 tests/ 以 runtime/ 为 sys.path 直接跑
    from filelock import locked
    from paths import project_root, safe_name

TURN_STATE_PATH = os.path.join(project_root(), ".turn_state.json")
_LOCK_NAME = ".turn_state.lock"

SCHEMA_VERSION = 2
EVIDENCE_KINDS = ("req", "art", "exp")
# 编号提示有效期：覆盖单个实验回合即可；太长会提示早已无关的旧证据。
FRESH_SECONDS = 1800
_REF_RE = {
    "req": lambda r: r.startswith("req_"),
    "art": lambda r: r.startswith("art-"),
    "exp": lambda r: r.startswith("exp-"),
}


def _lock_path() -> str:
    return os.path.join(os.path.dirname(TURN_STATE_PATH), _LOCK_NAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    """读入并规范化为 v2 形态。文件缺失/损坏/旧 schema → 从空状态重建（不抛）。"""
    empty = {"version": SCHEMA_VERSION, "turns_without_tool_use": 0, "evidence": {}}
    try:
        with open(TURN_STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return empty
        turns = data.get("turns_without_tool_use", 0)
        evidence = data.get("evidence", {})
        if not isinstance(turns, int) or turns < 0:
            turns = 0
        if not isinstance(evidence, dict):
            evidence = {}
        return {"version": SCHEMA_VERSION, "turns_without_tool_use": turns, "evidence": evidence}
    except (OSError, ValueError):
        return empty


def _atomic_write(data: dict) -> None:
    directory = os.path.dirname(TURN_STATE_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = TURN_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, TURN_STATE_PATH)


def record(target: str, kind: str, ref: str) -> None:
    """记录某 target 最近产出的证据编号。任何异常静默吞掉（热路径不得中断）。

    参数:
        target: 工具调用时的 target 身份串（键经 paths.safe_name 纯转换）。
        kind:   "req" | "art" | "exp"。
        ref:    编号字符串（如 req_142 / art-005 / exp-008），须非空且形态匹配。
    """
    try:
        if not target or kind not in EVIDENCE_KINDS:
            return
        ref = (ref or "").strip()
        if not ref or not _REF_RE[kind](ref):
            return
        key = safe_name(target)
        with locked(_lock_path()):
            data = _load()
            bucket = data["evidence"].setdefault(key, {})
            # 只保留该 kind 的最近一条；旧条目自然被覆盖，不做历史。
            bucket[kind] = {"ref": ref, "ts": _now_iso()}
            _atomic_write(data)
    except Exception:
        # 提示丢失只退回旧体验（无编号提示），绝不影响证据流本身。
        return


def _age_seconds(ts: str):
    try:
        written = datetime.fromisoformat(ts)
        if written.tzinfo is None:
            written = written.replace(tzinfo=timezone.utc)
        return max(0, int(time.time() - written.timestamp()))
    except (ValueError, TypeError):
        return None


def recent(*targets: str, within_seconds: int = FRESH_SECONDS) -> dict:
    """取一个或多个 target 键的最近新鲜证据。

    多个键用于 merge 侧同时尝试「现存世界目录名」与「身份转换名」（两者历史上可能不同），
    同 kind 取更新的一条。返回: {"req": (ref, age_s)|None, ...}。文件异常 → 全 None。
    """
    out = {kind: None for kind in EVIDENCE_KINDS}
    try:
        data = _load()
        evidence = data.get("evidence", {})
        keys = []
        for target in targets:
            if target:
                keys.append(safe_name(target))
        for key in dict.fromkeys(keys):  # 去重保序
            bucket = evidence.get(key)
            if not isinstance(bucket, dict):
                continue
            for kind in EVIDENCE_KINDS:
                item = bucket.get(kind)
                if not isinstance(item, dict):
                    continue
                ref = str(item.get("ref", "")).strip()
                age = _age_seconds(str(item.get("ts", "")))
                if not ref or age is None or age > within_seconds:
                    continue
                current = out[kind]
                if current is None or age < current[1]:
                    out[kind] = (ref, age)
    except Exception:
        return {kind: None for kind in EVIDENCE_KINDS}
    return out


def reset() -> None:
    """换 target 时机械清零（保持 merge 既有语义）。异常静默。"""
    try:
        with locked(_lock_path()):
            _atomic_write({"version": SCHEMA_VERSION, "turns_without_tool_use": 0, "evidence": {}})
    except Exception:
        return
