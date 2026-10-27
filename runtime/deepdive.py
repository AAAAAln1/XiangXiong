#!/usr/bin/env python3
"""deepdive.py — 邻域切片器（派发前的机械准备）。

**本模块不再派发。** 派发归宿主原生的 Task 工具（`.claude/agents/deepdive.md`）。

旧版自己起一个嵌套 claude 子进程并**阻塞等待**它跑完（默认 900s），有两个致命问题：
① 主 agent 在等待期完全瘫痪，物理上不可能并行；② 绕开宿主原生的并行调度与完成通知。
实测结果是派发次数 = 0。改成一"条消息里发 N 个 Task"之后，并行才是真的。

职责（纯机械，零判断）：
- 由 intent-id 从图取该前沿项，剪出**邻域切片**（不读全量世界）
- 邻域落文件，探索者的 prompt 只带路径（学 Cairn 的图快照：图多大都不占 prompt）
- 打印 JSON：路径 + 规模 + 边界声明，供探索者与主代理引用

不职责：
- 不派发（归 Task 工具）
- 不认领（归 intents.py）
- 不建实验（归 experiment.py，探索者自己建自己那条）
- 不写 memory.md（L3 单写者 = 主代理）

用法（探索者的第一步）：
  python runtime/deepdive.py <target> --intent h:xxxxxx

出参：
  {"ok": true, "target": "...", "intent": "h:xxxxxx",
   "neighborhood": "results/<target>/neighborhoods/h-xxxxxx.txt",
   "related": 12, "excluded": 3, "outside": 181,
   "anchors": ["/api/user", "..."]}
"""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    from .graph import parse as graph_parse
    from .graph import neighborhood, render_neighborhood, safe_id
    from .paths import world_active
    from .paths import safe_name as safe_target
except ImportError:
    from graph import parse as graph_parse
    from graph import neighborhood, render_neighborhood, safe_id
    from paths import world_active
    from paths import safe_name as safe_target

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.environ.get("XIANGXIONG_RESULTS_DIR") or os.path.join(PROJECT_ROOT, "results")
NEIGHBORHOOD_SUBDIR = "neighborhoods"

# 渲染上限：邻域行的展示条数。探索者需要的上下文比投影读者多，
# 但仍必须是有界的——无界切片等于把全量 memory 塞进它的开场。
RENDER_LIMIT = 20

INTENT_HINT = (
    "intent id 取自投影的 [FRONTIER] / [SWEEP] 段（形如 h:xxxxxx）。"
    "先确认 target 的世界文件里确有该行；不要凭 hypothesis 原文猜 id。"
)


def _as_posix(path: str) -> str:
    """子 agent 会用这个路径去 Read，统一正斜杠（Windows 反斜杠在提示里易被转义）。"""
    return path.replace("\\", "/")


def memory_path_for(target: str) -> str:
    """该 target 的世界文件路径。

    活跃 memory 的 target 与请求一致 → 用活跃（最新）；否则用归档。
    不推断历史、不判断继承关系，只做身份比对。
    """
    active = world_active()
    if os.path.exists(active):
        try:
            with open(active, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.strip().startswith("target:"):
                        if line.split(":", 1)[1].strip() == target:
                            return active
                        break
        except OSError:
            pass
    return os.path.join(RESULTS_DIR, safe_target(target), "memory.md")


def write_neighborhood(target: str, intent_id: str) -> tuple:
    """为 intent 剪出邻域并落文件。返回 (path, neighborhood)。

    无该 target 的世界文件、或该 intent 不在其中 → 返回 (None, None)，
    由调用方给出可执行的失败信息（不静默）。
    """
    path = memory_path_for(target)
    if not os.path.isfile(path):
        return None, None
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        graph = graph_parse(handle.readlines())
    found = neighborhood(graph, intent_id)
    if found is None:
        return None, None
    out_dir = os.path.join(RESULTS_DIR, safe_target(target), NEIGHBORHOOD_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, safe_id(intent_id) + ".txt")
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_neighborhood(found, limit=RENDER_LIMIT))
    return out_path, found


def fail_actionable(target: str, error: str, hint: str) -> None:
    """可执行失败：给出**对应该错误**的下一条命令，而不是通用提示或裸 argparse usage。

    提示必须与错误匹配 —— 指错方向的提示和静默失败一样没用。
    """
    print(json.dumps({
        "ok": False,
        "deepdive": "error",
        "target": target,
        "error": error,
        "hint": hint,
    }, ensure_ascii=False, indent=2))
    sys.exit(1)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="剪出一个前沿项的邻域切片（派发前的机械准备，不派发）"
    )
    parser.add_argument("target", help="结果归档目标名")
    parser.add_argument("--intent", required=True,
                        help="前沿项 ID（投影 [FRONTIER]/[SWEEP] 段的 h:xxxxxx）")
    args = parser.parse_args()

    intent_id = args.intent.strip()
    path, found = write_neighborhood(args.target, intent_id)
    if path is None:
        fail_actionable(
            args.target,
            f"intent 不存在于该 target 的世界: {intent_id}"
            f"（已查: {_as_posix(memory_path_for(args.target))}）",
            INTENT_HINT,
        )

    print(json.dumps({
        "ok": True,
        "target": args.target,
        "intent": intent_id,
        "intent_text": found.intent.text,
        "neighborhood": _as_posix(path),
        "related": len(found.related),
        "excluded": len(found.exclusions),
        # 空切片是**正常**的（刚写的 intent 还没有共享锚点的邻居），但必须显式标出来：
        # 否则读者分不清"真的没邻居"和"切片生成失败"。
        "empty_slice": len(found.related) == 0,
        "outside": found.outside,
        "anchors": sorted(found.anchors),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
