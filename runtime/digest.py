#!/usr/bin/env python3
"""digest.py — 决策摘要（decision digest）：投影的「去哪 + 做什么」两段。

## 为什么单独成模块

投影（`project.py`）是**会话开始时注入一次**的。之后 agent 每记一笔账，世界就变了，
而投影不变 —— 这就是这套架构最大的一个洞：调用方在凭一个**自知过期**的视图做决策。

补法不是"每轮重投影"（4,800 字符 × 每轮 = 灾难，且 v0.3 明确删掉了 PostToolUse），
而是**记账后按需回吐一个浓缩视图**：只在世界真正变了的时候，把"现在该做什么"再给一次。

这要求**同一段渲染逻辑**同时被两个场景使用：

| 场景 | 何时 | 用哪段 |
|---|---|---|
| SessionStart 投影 | 会话开始一次 | `[GOAL]` 与 `[NEXT]` 各就其位（见 `project._compose_sections`） |
| 记账后回吐 | 世界变了才回吐 | `format_decision_digest` = 上面两段 + 一行形状计数 |

**所以本模块存在的理由不是"拆文件"，是"不许出现第二份实现"。** 若回吐另写一份
`[NEXT]` 渲染，两处必然会漂移 —— 而本仓库已经在这件事上翻过两次车
（`protocol_scan`/`diff_scan` 的 status 归一化；`EVIDENCE_BLOCK_RE` 的三份副本，
后者的完整记录见 `graph.py` 顶部）。`tests/test_merge_digest.py` 逐字节断言
"同一 world 下，回吐里的 `[GOAL]`/`[NEXT]` 与投影里的完全相同"。

零判断：只排序（机械）、计数（机械）、截断（机械）。不评估、不推荐、不选下一步。
"""

from __future__ import annotations

import re

try:
    from .graph import (
        EVIDENCE_BLOCK_RE,
        KIND_EDGE,
        KIND_INTENT,
        node_id,
        unexplained_observations,
    )
    from .world import ParsedLine, WorldView
except ImportError:
    from graph import (
        EVIDENCE_BLOCK_RE,
        KIND_EDGE,
        KIND_INTENT,
        node_id,
        unexplained_observations,
    )
    from world import ParsedLine, WorldView

# hypothesis 状态的机械划分（不新增分类语义，只是把已有 state 字段分两桶）。
# 与 world.py 的 _HYPOTHESIS_STATES 同源；settled = 已闭合，其余 = 活前沿。
SETTLED_STATES = frozenset({"confirmed", "contradicted"})

# `sweep` = 批量机械验证（枚举/爆破/限流/逐条比对）：未闭合，但执行方式是"派发"。
# 它从 [FRONTIER] 里分出来单独成段，见 project.format_sweep_section。
SWEEP_STATE = "sweep"

# `[NEXT]` 每次最多给几条可抄的调用。投影与回吐用**同一个数**：
# 两处不同的上限会让人以为"回吐漏了几条"，而它其实只是个显示预算。
NEXT_LIMIT = 3


def brief(raw: str, limit: int = 120) -> str:
    """把一行的**正文**截到 limit，但**证据块原样保留**在尾部。

    为什么需要它（投影审计 2026-09-18）：`[EXCLUDED]` / `[SETTLED]` / `[UNEXPLAINED]`
    每行 190-235 字符，其中大半是"当时怎么测的"的过程描述。
    而这三段的决策价值只有两件事：**结论是什么** + **ID/证据在哪**。

    所以：正文截断（够读就行），证据引用逐字符保留（它是查证凭证，
    截断或改号都会让读者查不到账）。这不是"丢信息"—— 详情永远在 memory 里，
    投影只负责让读者知道"有这么一条、结论是什么、去哪查"。

    幂等：够短的行**原样返回**。摘出再拼回会引入多余间隔，
    于是同一行经过 brief 两次就长得不一样 —— 那会让 Phase-0 这类逐字节断言失去意义。
    """
    flat = " ".join(raw.split())
    if len(flat) <= limit:
        return flat
    refs = EVIDENCE_BLOCK_RE.findall(raw)
    body = EVIDENCE_BLOCK_RE.sub("", raw)
    body = " ".join(body.split())
    if len(body) > limit:
        body = body[:limit].rstrip() + "…"
    tail = " ".join(refs)
    return f"{body}  {tail}".strip() if tail else body


def hypothesis_claim_text(item: ParsedLine) -> str:
    """去掉 hypothesis 前缀，只留 claim 正文。"""
    match = re.match(r"^hypothesis\s*(?:\[[a-zA-Z]+\])?:\s*(.*)$", item.raw)
    return match.group(1).strip() if match else item.raw


def hypothesis_id(item: ParsedLine) -> str:
    """hypothesis 行的稳定 ID（与 graph.py 单一实现，补证据/升级状态不改号）。"""
    return node_id(KIND_EDGE, item.raw)


def dispatch_queue(view: WorldView) -> tuple:
    """此刻可派的项，按 Cairn dispatcher 的机械序排好。→ (全部, sweep 子集, open 子集)

    排序规则**完全机械**，照抄 Cairn 的 dispatcher：
        unclaimed = [i for i in intents if i.worker is None ...]
        newest = max(unclaimed, key=lambda i: i.created_at)   # 最新的未认领项优先
    这里不做任何价值判断 —— 只按 memory 写入序倒排（后写的在前）；sweep 整体优先，
    因为它的执行方式天然就是派发。

    两条机械排除：
    - `[blocked]`：缺验证条件（账号/权限/环境）。**派出去也做不了**，
      把这类项留在可派队列里等于教读者去撞墙。
    - `[confirmed]` / `[contradicted]`：已闭合，在 `[SETTLED]`。
    """
    sweep_items = [i for i in view.unknowns if i.state == SWEEP_STATE]
    open_items = [
        i for i in view.unknowns
        if (i.state or "") not in SETTLED_STATES
        and i.state != SWEEP_STATE
        and i.state != "blocked"
    ]
    # 最新的在前（Cairn：max(created_at)）；memory 是追加序，故直接倒排。
    ordered = list(reversed(sweep_items)) + list(reversed(open_items))
    return ordered, sweep_items, open_items


def format_next_section(view: WorldView, *, limit: int = NEXT_LIMIT) -> str:
    """`[NEXT]` —— 此刻可做的事，每条一行、可直接复制。

    为什么需要它（投影审计 2026-09-18）：投影原先只回答"世界长什么样"，
    **不回答"我现在该做什么"**。读者得自己从 `[FRONTIER]` / `[SWEEP]` / `[EXCLUDED]`
    推导出下一步 —— 而对 Flash 级模型（13B 激活），这一跳正是最容易失手的地方：
    它要么卡在一个做不了的项上，要么在若干段之间反复横跳。

    没有可做项时**也输出**并说明原因 —— 空段会被读成"这机制不存在"，
    而"你没有可派的活，去扩未知"恰恰是最该说出口的一句话。
    """
    if not view.target:
        return ""

    ordered, _, _ = dispatch_queue(view)

    if not ordered:
        blocked = [i for i in view.unknowns if i.state == "blocked"]
        detail = ""
        if blocked:
            detail = f"[FRONTIER] 里 {len(blocked)} 项为 [blocked]（缺验证条件，派了也做不了）"
        return (
            f"[NEXT] 无可派项{(' — ' + detail) if detail else ''}。"
            "下一步去 [UNEXPLAINED]（有观察没人问）或 [COVERAGE]（未做差分的路径）扩未知面。\n"
            "\n"
        )

    lines = [f"[NEXT] {len(ordered)} 项可派 — 同一条消息发多个 Agent 调用即并行："]
    for item in ordered[:limit]:
        intent_id = hypothesis_id(item)
        hint = brief(hypothesis_claim_text(item), 34)
        lines.append(
            f'  Agent(subagent_type="deepdive", prompt="target={view.target} '
            f'intent={intent_id}")   # {hint}'
        )
    if len(ordered) > limit:
        lines.append(f"  (+{len(ordered) - limit} more，全部见 [FRONTIER] / [SWEEP])")
    return "\n".join(lines) + "\n\n"


def dispatch_hint(target: str) -> str:
    """派发样板：一条**工具名正确**的可复制调用。

    工具名必须是宿主注册的那个（`Agent`），不是它的兼容别名（`Task`）。
    `Task` 只用于权限规则/钩子/恢复会话的向后兼容，**不是**可调用的工具名——
    写错会让照抄的一方去调一个不存在的工具（实测：主 agent 自己纠正成了 `Agent`，
    但那是它在替我们擦屁股，不能依赖）。

    只在有 target 时给模板（无 target 时没有可派的会话上下文）。
    模板本身不判断"该不该派"—— 派发的判据是机械的：这一项还没人做。
    """
    if not target:
        return ""
    return (
        f' — 派发（不阻塞你继续探索）：'
        f'Agent(subagent_type="deepdive", prompt="target={target} intent=<此行的 h:xxxxxx>")'
    )


def format_goal_section(view: WorldView) -> str:
    """`[GOAL]` —— 搜索的终止条件与阶段里程碑（`goal:` / `subgoal:`）。

    为什么这是**首段**：其余每一段都在回答"怎么走"，只有它回答"去哪"。
    没有它，`[FRONTIER]` / `[SWEEP]` 只是一串待办，读者无从判断"还差多少才收工"
    —— 而那是"搜索不发散、任务会结束"的唯一依据（实测：42 分钟 / 101 次 Bash
    的一次执行全程没有任何收工信号）。

    未声明时**不静默**，而是给一行可直接抄的写法：空段会被读成"这机制不存在"，
    而它恰好是最该先填的一格。这与 `[SWEEP]` 段头直接给派发样板是同一个道理 ——
    把动作降到"抄一行"。

    段头带 `target`：执行任何命令都要用到它，而原先它埋在第 5 段
    （实测首屏 1,128 字符处才首次出现）。首屏给不出"我在测哪个站"，
    读者就得先翻页才能动手 —— 对弱模型这是实打实的摩擦。
    """
    goals = [item.raw for item in view.objects if item.prefix == "goal"]
    subgoals = [item.raw for item in view.objects if item.prefix == "subgoal"]
    where = f"target={view.target}" if view.target else "target=(none)"
    # 归属声明（2026-09-25）：投影**只属于一个 target**，而"用户这一轮要测的是另一个站"
    # 是常态 —— 实测 5/5 个换目标会话开场都撞上（hook 给的是上一个目标的世界，
    # 读者要自己发现、自己试错切目标，平均花 1–3 个失败动作）。
    # 这一行把"归属 + 补救动作"放在第一行（该段永不裁剪），把发现成本降到 0。
    ownership = (
        f" · 本投影只属于 {view.target}；换站先写 DELTA: target: <新站>"
        if view.target else " · 世界还没有 target，先写 DELTA: target: <站>"
    )
    if not goals and not subgoals:
        return (
            f"[GOAL] {where} · 未声明完成条件 — 先写，否则无法判断何时收工：\n"
            "  goal: <什么算完成，且能被证据判定>\n"
            "  subgoal: <阶段里程碑，可随时增删>\n"
            f"{ownership.strip(' ·')}\n"
            "\n"
        )
    count = f"{len(goals)} goal"
    if subgoals:
        count += f", {len(subgoals)} subgoal"
    lines = [f"[GOAL] {where} · {count}{ownership}"]
    lines += [f"  {raw}" for raw in goals]
    lines += [f"  {raw}" for raw in subgoals]
    return "\n".join(lines) + "\n\n"


def format_decision_zone(view: WorldView, *, limit: int = NEXT_LIMIT) -> str:
    """「去哪 + 做什么」两段（`[GOAL]` + `[NEXT]`）。

    这是**回吐的判据**：只有它变了，才说明世界的"下一步"真变了，才值得打扰读者。
    只加一条 `surface:` 不会动它；新增/闭合一条 hypothesis、改一次 goal 会。
    """
    return format_goal_section(view) + format_next_section(view, limit=limit)


def format_world_shape(view: WorldView, graph=None) -> str:
    """一行形状计数 —— 「我走到哪了」的最小读数。

    只数四件已经在别处定义过的事，不引入新概念：
    可派（含 sweep / open 拆分）、已闭合、已排除、未解释。
    （请求数 / 路径数属 [COVERAGE]，要读磁盘，故不进这个纯函数。）
    """
    if not view.target:
        return ""
    ordered, sweep_items, open_items = dispatch_queue(view)
    settled = [i for i in view.unknowns if (i.state or "") in SETTLED_STATES]
    unexplained = len(unexplained_observations(graph)) if graph is not None else 0
    return (
        f"可派 {len(ordered)}（sweep {len(sweep_items)} / open {len(open_items)}）"
        f" · 闭合 {len(settled)} · 排除 {len(view.counters)} · 未解释 {unexplained}\n"
    )


def format_decision_digest(view: WorldView, graph=None, *, limit: int = NEXT_LIMIT) -> str:
    """回吐主体：`[GOAL]` + `[NEXT]` + 一行形状。

    刻意**不含** `[WORLD MODEL]` / `[SETTLED]` / `[EXCLUDED]` 正文 —— 那些是背景与历史，
    重看它们的时机是"我需要查账"，不是"我刚记了一笔账"。
    真要看全文有一行指针（见 merge 的回吐段头）。
    """
    zone = format_decision_zone(view, limit=limit)
    shape = format_world_shape(view, graph)
    return zone + shape if shape else zone
