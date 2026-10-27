#!/usr/bin/env python3
"""
project.py — Phase 1.3 Projection Renderer (H0–H15).

compile() is a stable generator of the former session_start stdout.
Ownership move only: no new projection semantics, no parameters.
Does not read host chat streams as projection input.

Memory structure comes only from world.parse() (single extract source).
OPEN/STALLED and related display rules live only in this renderer (H9 legacy).

静态宪法（认知模型 / 反压缩）不在这里：它是会话不变量，归 CLAUDE.md 由宿主注入；
本模块只投影**动态世界状态**，并自限于 PROJECTION_BUDGET_CHARS，避免被宿主硬截断。

Naming: Projection Renderer / Context Projection.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from urllib.parse import urlparse

try:
    from .digest import (
        NEXT_LIMIT,
        SETTLED_STATES,
        SWEEP_STATE,
        brief,
        dispatch_hint,
        format_goal_section,
        format_next_section,
        hypothesis_claim_text,
        hypothesis_id,
    )
    from .graph import (
        KIND_ASSET,
        KIND_CONSTRAINT,
        KIND_EDGE,
        KIND_EVIDENCE,
        KIND_FINDING,
        KIND_GOAL,
        KIND_INTENT,
        KIND_OBSERVATION,
        KIND_ORDER,
        node_id,
        parse as graph_parse,
        terrain,
        unexplained_observations,
    )
    from .paths import project_root, world_active
    from .world import ParsedLine, WorldView, parse as world_parse
    from .merge import target_identity
except ImportError:
    from digest import (
        NEXT_LIMIT,
        SETTLED_STATES,
        SWEEP_STATE,
        brief,
        dispatch_hint,
        format_goal_section,
        format_next_section,
        hypothesis_claim_text,
        hypothesis_id,
    )
    from graph import (
        KIND_ASSET,
        KIND_CONSTRAINT,
        KIND_EDGE,
        KIND_EVIDENCE,
        KIND_FINDING,
        KIND_GOAL,
        KIND_INTENT,
        KIND_OBSERVATION,
        KIND_ORDER,
        node_id,
        parse as graph_parse,
        terrain,
        unexplained_observations,
    )
    from paths import project_root, world_active
    from world import ParsedLine, WorldView, parse as world_parse
    from merge import target_identity

# ── 本模块只负责**世界状态**的那些段（TERRAIN / FINDINGS / FRONTIER / SWEEP /
# EXCLUDED / WORLD MODEL / UNEXPLAINED / TOOLS / COVERAGE / DIFFS / 实验 / ARTIFACTS）。
# 「去哪 + 做什么」（[GOAL] / [NEXT]）与它们的压缩原语 `brief` 归 digest.py ——
# 因为**记账后回吐**要用同一份实现，两处各写一份必然漂移（见 digest.py 顶部）。

# ── 投影预算（对齐宿主硬上限）────────────────────────────────────────────────
# 宿主对 SessionStart hook 的 additionalContext 有**硬上限**：超出即不再注入原文，
# 只注入前 2,000 字符预览（claude-code 2.1.x：Vz8(additionalContext, hookId,
# "additionalContext")，缺省阈值 pP4 = 10000，预览长度 KL6 = 2000）。
#
# 实测事故（2026-09-17，首次真实执行）：投影 10,408 字符 → 超限 → 宿主只注入
# 前 2,000 字符，而那正好全是 [COGNITION] → [TERRAIN]/[FRONTIER]/[EXCLUDED]/
# [WORLD MODEL] 一个都不可见 → agent 开局没有可认领项 → 认领/实验/深挖全部归零。
#
# 所以投影必须**自限**：不依赖宿主行为，超限即按固定顺序机械裁剪，并把
# 裁剪行数与全文位置**显式写进投影**——静默丢失比截断更糟。
# 留 10% 余量，因为预算是字符数而宿主按 UTF-16 长度判定。
PROJECTION_BUDGET_CHARS = 9000

# 段名 → 被裁剪时指向全文的诚实位置。只指向**已存在**的文件或命令，不产生新文件。
SECTION_RECOVERY = {
    "SETTLED": ".claude/memory.md",
    "OPEN EXPERIMENTS": "results/<target>/experiments/",
    "RECENT EXPERIMENTS": "results/<target>/experiments/",
    "DIFFS": "python runtime/diff_scan.py <target>",
    "UNEXPLAINED": ".claude/memory.md",
    "WORLD MODEL": ".claude/memory.md",
    "EXCLUDED": ".claude/memory.md",
    "FRONTIER": ".claude/memory.md",
    "SWEEP": ".claude/memory.md",
    "FINDINGS": ".claude/memory.md",
}

# 裁剪顺序：**先牺牲历史/背景，最后才动交付物与可派队列**。
# TERRAIN / COVERAGE / RESUME / ARTIFACTS 不在表内 —— 它们是百字节量级的判据，永远不裁。
#
# 2026-09-25 实测改序（会话 c63bb02b / c6d8fa59 的投影字节数）：
#   [EXCLUDED] 4,586（51%）+ [FINDINGS] 1,732（19%）= 70% 的预算给了"结论与历史"，
#   而 [UNEXPLAINED]/[DIFFS]/[SETTLED]/[RECENT] 四段被压成只剩段头（合计 497 字符）。
#   直接后果：diff_scan.py 在 20 个会话里被调用 0 次 —— 指针没人跟。
# 故把**最长的历史段（EXCLUDED）放到第一位**牺牲，并让 [UNEXPLAINED]/[DIFFS]
# 排在 [WORLD MODEL] 之后（它们原先比背景段还先死）。
# 既有不变量保持：FINDINGS（交付物）仍最后；SETTLED/DIFFS/WORLD MODEL/FRONTIER/SWEEP
# 都在 FINDINGS 之前（见 tests/test_goal_and_findings.py）。
SECTION_TRIM_ORDER = (
    "EXCLUDED",
    "SETTLED",
    "RECENT EXPERIMENTS",
    "OPEN EXPERIMENTS",
    "WORLD MODEL",
    "UNEXPLAINED",
    "DIFFS",
    "FRONTIER",
    "SWEEP",
    "FINDINGS",
)

_ELISION_PREFIX = "  ... "


def _elision_marker(dropped: int, where: str) -> str:
    """裁剪标记：写明丢了几行、全文在哪。空标记（dropped<=0）不占字符。"""
    if dropped <= 0:
        return ""
    if where:
        return f"{_ELISION_PREFIX}+{dropped} lines dropped by projection budget; full: {where}\n"
    return f"{_ELISION_PREFIX}+{dropped} lines dropped by projection budget\n"


def _trim_section(text: str, allowed: int, where: str):
    """把一段裁到 <= allowed 字符：保留段头 + 尽量长的正文前缀 + 裁剪标记。

    只做机械的尾部裁剪——不摘要、不重排、不评估内容。
    返回 None 表示该段无可裁（段头 / 单行 / 已裁过 / 裁了反而更长），
    调用方据此换下一段，而不是在这里兜底。
    """
    lines = text.splitlines(keepends=True)
    if len(lines) < 3:
        return None
    header, body = lines[0], lines[1:]
    tail = ""
    if body and not body[-1].strip():
        tail, body = body[-1], body[:-1]
    if not body or body[-1].startswith(_ELISION_PREFIX):
        return None
    best = None
    for keep in range(len(body) - 1, -1, -1):
        candidate = (
            header
            + "".join(body[:keep])
            + _elision_marker(len(body) - keep, where)
            + (tail or "\n")
        )
        best = candidate
        if len(candidate) <= allowed:
            return candidate
    return best if best is not None and len(best) < len(text) else None


def _enforce_budget(sections: list, budget: int) -> list:
    """按 SECTION_TRIM_ORDER 机械裁剪，使总长 <= budget。

    外层多轮是必要的：一段被裁到最小形态仍不够时，由下一段继续承担。
    无进展即停（防死循环），不做兜底丢弃——宁可略微超预算也不静默删段。
    """
    out = list(sections)
    for _ in range(len(SECTION_TRIM_ORDER) + 1):
        total = sum(len(text) for _, text in out)
        if total <= budget:
            break
        progressed = False
        for name in SECTION_TRIM_ORDER:
            total = sum(len(text) for _, text in out)
            if total <= budget:
                break
            for index, (section_name, text) in enumerate(out):
                if section_name != name:
                    continue
                trimmed = _trim_section(
                    text, len(text) - (total - budget), SECTION_RECOVERY.get(name, "")
                )
                if trimmed is not None:
                    out[index] = (section_name, trimmed)
                    progressed = True
                break
        if not progressed:
            break
    return out


def _results_dir() -> str:
    return os.environ.get("XIANGXIONG_RESULTS_DIR") or os.path.join(project_root(), "results")


def _target_safe_name(target: str) -> str:
    """路径拼接唯一权威 paths.safe_name 的薄封装（2026-10-06 收敛，勿再写 replace 副本）。"""
    try:
        from .paths import safe_name
    except ImportError:
        from paths import safe_name
    return safe_name(target)


def _load_recent_experiments(target: str, limit: int = 5) -> list:
    target_id = target_identity(target) if target else target
    root = os.path.join(_results_dir(), _target_safe_name(target_id), "experiments")
    if not os.path.isdir(root):
        return []

    experiment_ids = sorted(
        name
        for name in os.listdir(root)
        if name.startswith("exp-") and os.path.isdir(os.path.join(root, name))
    )
    experiments = []
    for experiment_id in experiment_ids:
        path = os.path.join(root, experiment_id, "experiment.json")
        try:
            with open(path, "r", encoding="utf-8") as file:
                metadata = json.load(file)
        except (OSError, json.JSONDecodeError):
            continue
        metadata["_sort_id"] = experiment_id
        experiments.append(metadata)
    experiments.sort(
        key=lambda item: (item.get("created_at", ""), item.get("experiment_id", item["_sort_id"])),
    )
    active_id = None
    try:
        with open(os.path.join(root, ".active"), "r", encoding="utf-8") as file:
            candidate = file.read().strip()
        active = next(
            (
                item
                for item in experiments
                if item.get("experiment_id", item["_sort_id"]) == candidate
            ),
            None,
        )
        if active is not None and not active.get("closed"):
            active_id = candidate
            active = dict(active, active=True)
    except OSError:
        active = None
    others = [
        item
        for item in experiments
        if item.get("experiment_id", item["_sort_id"]) != active_id
    ]
    if active is not None and active_id is not None:
        return [active] + others[-max(0, limit - 1) :]
    return others[-limit:]


def _shorten(value, limit: int = 240) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "..."


def _format_recent_experiments(target: str) -> str:
    """[RECENT EXPERIMENTS] —— 已完成的实验，**一行一个**。

    只给「ID + 状态 + 结论 + 证据路径」。原实现每个实验渲染 8 行，其中：

    - `observation:` 是**命令执行元数据**（`command: python … status: success
      exit_code: 0 duration: 53.602s reason: None metadata_artifact: …`）——
      对"下一步做什么"零增量，实测占该段一半以上篇幅
    - `artifact:` 与 `evidence:` 重复（都是路径），`closed:` 与段名重复
    - `hypothesis:` 在 [FRONTIER] / [SETTLED] 里已经有了

    结论是这一段**唯一**的增量信息：假设在 FRONTIER/SETTLED，过程在 evidence.md。
    实测该段 1,484 字符（占投影 23%），压成一行后约 350。
    """
    if not target:
        return ""

    experiments = _load_recent_experiments(target)
    if not experiments:
        return ""
    # 段头与状态必须如实（2026-09-25 审计）：_load_recent_experiments 不按 closed 过滤，
    # 旧文案"已完成"会把未关闭实验谎报成已完成，且与 [OPEN EXPERIMENTS] 自相矛盾；
    # status 取值处的 fallback 是死代码（create 恒写 'unknown'，truthy）。
    lines = [f"[RECENT EXPERIMENTS] ({len(experiments)}) — 最近实验（open=未关闭），详情见 experiments/"]
    for metadata in experiments:
        experiment_id = metadata.get("experiment_id") or metadata.get("_sort_id") or "?"
        if metadata.get("closed"):
            status = metadata.get("status") or "unknown"
        else:
            status = "open"
        conclusion = brief(
            metadata.get("conclusion") or metadata.get("hypothesis") or "(无结论)", 130
        )
        lines.append(
            f"  {experiment_id} [{status}] {conclusion}  (experiments/{experiment_id}/evidence.md)"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def _format_open_experiments(target: str) -> str:
    """
    v0.3.2: OPEN EXPERIMENTS 状态投影（认知持久化验证）。
    
    设计原则（严格）：
    - 只展示未关闭实验状态（有 experiment.json，无 conclusion.md）
    - 只展示当前 target
    - 不提醒关闭（无"建议""可能丢失"等引导语）
    - 不判断生命周期（不评估该不该关闭）
    - 不阻塞行为（LLM 可以忽略）
    
    投影内容：
    - experiment_id
    - observations 数量
    - 最后一个 artifact（若有）
    - hypothesis（若有）
    """
    exp_root = experiments_dir_for_target(target)
    if not os.path.isdir(exp_root):
        return ""
    
    open_experiments = []
    for exp_name in sorted(os.listdir(exp_root)):
        if not exp_name.startswith("exp-"):
            continue
        exp_dir = os.path.join(exp_root, exp_name)
        if not os.path.isdir(exp_dir):
            continue
        
        exp_json = os.path.join(exp_dir, "experiment.json")
        if not os.path.exists(exp_json):
            continue

        try:
            with open(exp_json, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        # 只展示未关闭实验。
        # 旧判据是「无 conclusion.md」，但 experiment.py close 只写 evidence.md，
        # 从不写 conclusion.md → 判据恒真 → 已关闭实验全部被标成未关闭（实测 127/129）。
        if metadata.get("closed"):
            continue

        # 收集状态字段（纯展示）
        fields = [f"id: {exp_name}"]
        
        observations = metadata.get("observations", [])
        obs_count = len(observations)
        fields.append(f"observations: {obs_count}")
        
        # 最后一个 artifact（若有）
        if observations and isinstance(observations[-1], dict):
            last_artifact = observations[-1].get("artifact")
            if last_artifact:
                fields.append(f"artifact: {_shorten(last_artifact, 80)}")
        
        # hypothesis（若有）
        hypothesis = metadata.get("hypothesis")
        if hypothesis:
            fields.append(f"hypothesis: {_shorten(hypothesis, 120)}")
        
        open_experiments.append("  " + " | ".join(fields))
    
    if not open_experiments:
        return ""
    
    lines = ["[OPEN EXPERIMENTS]"]
    lines.extend(open_experiments)
    lines.append("")
    return "\n".join(lines) + "\n"


def _read_memory_lines() -> list:
    path = world_active()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return strip_bom(lines)


def strip_bom(lines: list) -> list:
    """容忍 UTF-8 BOM：PowerShell 系工具（Set-Content -Encoding UTF8）写出的文件
    首字节是 EF BB BF，会让第一行变成 `\\ufefftarget: …` → 解析器认不出 target
    → 投影首行回落到 `target=(none)`，读者随即失去"我在测哪个站"。

    与 merge 侧的 BOM 容忍（tests/test_cli_ergonomics.TestBomTrapIsLoud）同一条判据：
    内容是对的，就不该因为 BOM 静默丢信息。只动首行的 BOM，不动别处。
    """
    if lines and lines[0].startswith("\ufeff"):
        lines = list(lines)
        lines[0] = lines[0].lstrip("\ufeff")
    return lines


def format_findings_section(view: WorldView) -> str:
    """[FINDINGS] —— 交付物（`vuln:`）。

    与 [WORLD MODEL] 分开：对渗透测试 / 代码审计，**搜索的产物是挖到的漏洞**，
    而不是"完成所有功能点测试与漏洞挖掘"这种最终 Goal。把交付物埋在 objects 里，
    等于要求读者自己从一堆事实里认出"哪些是能交付的"——实测中 `vuln:` 与
    `surface:` / `cred:` 混在同一段，没有任何结构提示它是产出。

    与 [GOAL] 构成唯一的一对读数量：Goal 说"要什么"，Findings 说"拿到了什么"。
    为空时不占位（"还没挖到"是正常状态，不需要催）。
    """
    findings = [item.raw for item in view.objects if item.prefix == "vuln"]
    if not findings:
        return ""
    # 逐条 brief（与 [EXCLUDED] 同口径）：vuln 行是**交付物**，投影要回答的是
    # "挖到了什么 + 证据在哪"，而不是复述当时怎么测的（平均 346 字符/行 → 130）。
    # 全文永远在 memory.md / report.md 里。
    lines = [f"[FINDINGS] ({len(findings)})"]
    lines += [f"  {brief(raw, 130)}" for raw in findings]
    return "\n".join(lines) + "\n\n"


# prefix → kind（供 [WORLD MODEL] 给每行加稳定 ID 用）。
# 与 graph.PREFIX_TO_KIND 同源，但按 world 的 prefix（无冒号）取值。
_PREFIX_KIND = {
    "surface": KIND_OBSERVATION,
    "vuln": KIND_FINDING,
    "cred": KIND_ASSET,
    "tech": KIND_CONSTRAINT,
    "port": KIND_CONSTRAINT,
    "artifact": KIND_EVIDENCE,
    "goal": KIND_GOAL,
    "subgoal": KIND_GOAL,
}


def _line_with_id(prefix: str, raw: str) -> str:
    """给一行加稳定 ID 前缀。ID 是引用凭证 —— [UNEXPLAINED] 靠它指回本行。"""
    kind = _PREFIX_KIND.get(prefix)
    return f"{node_id(kind, raw)} {raw}" if kind else raw


def _world_bucket_lists(view: WorldView) -> tuple[list[str], list[str], list[str]]:
    """Surface buckets for WORLD MODEL body (legacy SessionStart filters).

    `vuln` 已移出到 [FINDINGS]（它是交付物，不是世界模型里的一个事实大类）。
    """
    world_objects = [
        item.raw
        for item in view.objects
        if item.prefix in ("surface", "cred")
    ]
    world_constraints = [item.raw for item in view.constraints]
    world_counters = [item.raw for item in view.counters]
    return world_objects, world_constraints, world_counters


def format_world_section(view: WorldView) -> str:
    """
    World bucket section renderer (single world projection source, Phase 2.0 fix).

    Emits objects + constraints only（counters 已移出到 [EXCLUDED]：
    排除集是决策信息，不该埋在定局质量里 —— 规格 §6「定局压缩、活前沿展开」）。
    Does NOT emit unknowns/RECENT/RESUME/ARTIFACTS and does NOT decide ordering.
    """
    world_objects, world_constraints, _ = _world_bucket_lists(view)

    world_lines = ["[WORLD MODEL]"]
    if view.target:
        world_lines.append(f"target: {view.target}")
    else:
        world_lines.append("target: (none)")
    world_lines.append("")

    world_lines.append(f"objects ({len(world_objects)}):")
    for item in view.objects:
        # 只列 surface / cred：goal/subgoal 在 [GOAL]、vuln 在 [FINDINGS]、
        # future 在 [FRONTIER] 的 next: —— 同样的行出现在两处只会制造"这是新信息"的错觉。
        if item.prefix not in ("surface", "cred"):
            continue
        world_lines.append(f"  {_line_with_id(item.prefix, item.raw)}")
    world_lines.append("")

    world_lines.append(f"constraints ({len(world_constraints)}):")
    for item in view.constraints:
        world_lines.append(f"  {_line_with_id(item.prefix, item.raw)}")
    world_lines.append("")
    return "\n".join(world_lines) + "\n"


def format_excluded_section(view: WorldView) -> str:
    """[EXCLUDED] —— 排除集（counter / dead）。独立成段，位置紧跟 [FRONTIER]。

    这是最省时间的一类信息：读到"这一族已死"就不必再走。
    段头直说"勿重试"，段内每行只留**结论 + 证据 ID**（正文截断，见 digest.brief）——
    原先每行 190 字符里大半是"当时怎么测的"，对"别碰哪"没有增量。

    dead 拓扑三态分组（2026-10-06 P0-b）：filtered=位置条件态（换立足点重估）、
    closed=端口关闭（不重试）、unreachable=无路由（族级收口）。**无任何带状态行时
    输出与历史格式逐字节一致**（phase0 fixture 锁死）；组提示是投影层文本，不写进账本。
    """
    counters = list(view.counters)
    if not counters:
        return ""
    stated = [c for c in counters if getattr(c, "state", None)]
    if not stated:
        lines = [f"[EXCLUDED] ({len(counters)}) — 已排除方向，勿重试（详情见 memory）"]
        lines += [f"  {brief(c.raw, 130)}" for c in counters]
        return "\n".join(lines) + "\n\n"

    # 有三态行 → 分组渲染（counter 与无状态 dead 归 general）
    groups = [
        ("filtered", "filtered（位置条件态；换立足点/隧道后必须重估）"),
        ("closed", "closed（端口关闭；勿重试）"),
        ("unreachable", "unreachable（无路由；族级收口）"),
        ("__general__", "counter / 其他排除"),
    ]
    buckets = {key: [] for key, _ in groups}
    for c in counters:
        key = c.state if c.state in ("filtered", "closed", "unreachable") else "__general__"
        buckets[key].append(c)

    lines = [f"[EXCLUDED] ({len(counters)}) — 已排除方向（详情见 memory）"]
    for key, label in groups:
        items = buckets[key]
        if not items:
            continue
        lines.append(f"  {label} ({len(items)}):")
        lines += [f"    {brief(c.raw, 130)}" for c in items]
    return "\n".join(lines) + "\n\n"


def format_terrain_section(graph) -> str:
    """[TERRAIN] —— 形状计数（judge 的 J1）。放在最前：先看形状，再看细节。

    只数：某 kind 有多少、edge 各状态多少。零评估、零排序建议。
    （「无假设解释的观察数」不在这里 —— 它是 [UNEXPLAINED] 段头的计数，重复无益。）
    """
    rows = terrain(graph)
    if not rows:
        return ""
    by_kind = {}
    for kind, state, count in rows:
        by_kind.setdefault(kind, {"_total": 0, "_states": {}})
        by_kind[kind]["_total"] += count
        if state:
            by_kind[kind]["_states"][state] = count

    flat = []
    for kind in KIND_ORDER:
        if kind not in by_kind:
            continue
        entry = by_kind[kind]
        total = entry["_total"]
        stated = sum(entry["_states"].values())
        if entry["_states"] and stated == total:
            detail = " | ".join(f"{s} {n}" for s, n in sorted(entry["_states"].items()))
            flat.append(f"{kind} {total} ({detail})")
        elif entry["_states"]:
            # 有未标状态的行（如无状态标记的 hypothesis:）。显式列出，否则小计对不上总数。
            detail = " | ".join(f"{s} {n}" for s, n in sorted(entry["_states"].items()))
            flat.append(f"{kind} {total} ({detail} | none {total - stated})")
        else:
            flat.append(f"{kind} {total}")
    lines = ["[TERRAIN]", "  " + " | ".join(flat)]
    # 「vuln 无实验背书」：机械计数（只说"有几条 vuln 行里没有 exp-NNN"）。
    # 实测（9/17 盲跑会话）：5 条重洞全部只有 req 引用、0 条有 exp —— 而证据闸门
    # 只对 confirmed/contradicted 强制 exp，于是"跳过实验"成了合法绕道。
    # 这里只把数字说出来（不拦、不说教），让读者自己决定要不要补实验。
    unbacked = _vuln_without_experiment(graph)
    if unbacked:
        lines.append(f"  vuln_without_exp {unbacked}（vuln 行里没有 exp-NNN 引用；建议补一次实验）")
    return "\n".join(lines) + "\n\n"


def _vuln_without_experiment(graph) -> int:
    """vuln 行里不含 exp-NNN 引用的条数（纯计数）。"""
    if graph is None:
        return 0
    count = 0
    for node in getattr(graph, "nodes", ()):
        if node.kind != KIND_FINDING or not node.raw.startswith("vuln:"):
            continue
        if not re.search(r"exp-\d+", node.raw):
            count += 1
    return count


def format_unexplained_section(nodes, *, limit: int = 10) -> str:
    """[UNEXPLAINED] —— 锚点未被任何假设提及的观察（E5 / J4）。

    这是新框架的唯一种子：有观察、无人问。列出 ID 以便引用。

    段头直说"候选探索面"，因为这是它**唯一的用途**：当没有可派项时，
    这里是下一步该看的地方。原先段头只有一个计数，读者得自己猜它是干嘛的。

    行内正文截断到 90 字符（见 digest.brief）：这四行原文与 [WORLD MODEL] 逐字节相同，
    原样复制等于让读者以为有新信息 —— 它在世界模型里已经完整存在。
    """
    if not nodes:
        return ""
    # 只列 ID，不复制正文：[WORLD MODEL] 现在给每行都带 ID，ID 唯一指向那行。
    # 原先逐条复制 90 字符正文，等于让读者以为这里藏着新信息 —— 实际是同一批行。
    ids = " ".join(n.id for n in nodes[:limit])
    more = f"  (+{len(nodes) - limit} more)" if len(nodes) > limit else ""
    return (
        f"[UNEXPLAINED] ({len(nodes)}) — 这些观察还没有假设解释（候选探索面，正文见 [WORLD MODEL]）\n"
        f"  {ids}{more}\n"
        "\n"
    )


def format_frontier_section(view: WorldView) -> str:
    """[FRONTIER] —— 活前沿：未闭合的假设 + 显式声明的下一步（future:）。

    结构与 [SETTLED] 分离：已解决的行不再淹没活前沿。
    这是按**已有 state 字段**做的结构划分，不新增分类语义、不过滤任何 state
    （FRONTIER + SWEEP + SETTLED 合起来仍等于全量 unknowns，见 tests/test_session_start_parse.py）。

    段头带一条**可直接复制的派发指令**：这是"记账副产品派发"的落点（规格 §8.0g）。
    主 agent 写下 hypothesis 就等于写下了待派队列；这里把动作降到"抄一行"，
    而不是要求它先想起来"我是不是该派个子 agent"。
    """
    rows: list[str] = []
    for item in view.unknowns:
        if (item.state or "") in SETTLED_STATES:
            continue
        if item.state == SWEEP_STATE:
            continue
        label = f"  [{item.state}]" if item.state else ""
        rows.append(f"  hypothesis: {hypothesis_claim_text(item)}{label}  {hypothesis_id(item)}")
    for item in view.objects:
        if item.prefix == "future":
            text = item.raw[len("future:"):].strip()
            rows.append(f"  next: {text}  {node_id(KIND_INTENT, item.raw)}")
    if not rows:
        return ""
    header = f"[FRONTIER] ({len(rows)} open){dispatch_hint(view.target)}"
    return "\n".join([header] + rows) + "\n\n"


def format_sweep_section(view: WorldView) -> str:
    """[SWEEP] —— 批量机械验证类工作（`hypothesis [sweep]:`）。

    与 [FRONTIER] 的区别不是真假，而是**正确的执行方式**：
    枚举 / 爆破 / 限流测试 / 逐条比对需要 N 次重复发包，在主会话里做会把
    N 份响应全灌进主上下文，而且只能串行。这类工作的默认执行者是独立 context 的子 agent。

    这是一个**队列**，不是建议列表：入队判据完全机械（state == sweep）。
    新框架里这是唯二会主动要求"派发"的段（另一个是 [FRONTIER] 段头模板）。
    """
    rows: list[str] = []
    for item in view.unknowns:
        if item.state != SWEEP_STATE:
            continue
        rows.append(f"  hypothesis: {hypothesis_claim_text(item)}  {hypothesis_id(item)}")
    if not rows:
        return ""
    header = (
        f"[SWEEP] ({len(rows)} batch){dispatch_hint(view.target)}"
        " — 批量机械验证，默认派发：别在主会话里串行跑 N 次"
    )
    return "\n".join([header] + rows) + "\n\n"


def format_settled_section(view: WorldView) -> str:
    """[SETTLED] —— 已闭合的假设（confirmed / contradicted），保留全量可见。

    段头直说"勿重开"：这是它唯一的用途 —— 防止读者把已经证过/证伪的东西再测一遍。
    每行只留结论 + 状态 + ID（正文截断，见 digest.brief）；原先每行 235 字符，
    其中一半是逐条列出的 req 编号，对"这条已经定了"没有增量。
    """
    settled = [i for i in view.unknowns if (i.state or "") in SETTLED_STATES]
    if not settled:
        return ""
    counts = Counter(i.state for i in settled)
    header = " ".join(f"{state}={counts[state]}" for state in sorted(counts))
    rows = [
        f"  hypothesis: {brief(hypothesis_claim_text(i), 140)}  [{i.state}]  {hypothesis_id(i)}"
        for i in settled
    ]
    return "\n".join([f"[SETTLED] ({header}) — 已闭合，勿重开（详情见 memory）"] + rows) + "\n\n"


def experiments_dir_for_target(target: str) -> str:
    """Shared experiment directory path (no third ad-hoc root)."""
    target_id = target_identity(target) if target else target
    return os.path.join(_results_dir(), _target_safe_name(target_id), "experiments")


def _target_identity_safe(target: str) -> str:
    """target 描述 → results 目录名（merge.target_identity 抽纯域名 → safe name）。"""
    target_id = target_identity(target) if target else target
    return _target_safe_name(target_id)


# ── [TOOLS] 本机工具就绪（缺失面）────────────────────────────────────────────
# 只列**缺失**的外部二进制：缺失是本机事实，会改变下一跳能不能执行；
# 已存在的工具不列出 —— 列全量即成为"工具菜单"，会被读成推荐（边界见 runtime/toolcheck.py）。
# 故意不进 SECTION_TRIM_ORDER：单行、百字节量级、且属判据（与 COVERAGE 同级）。
# 名字过多时截断到 TOOLS_NAMES_IN_PROJECTION 个（6 个足够提示"缺一批"），
# 并显式写出还剩几个与全文位置 —— 与预算裁剪同一原则：不静默丢信息。
# 探针模块缺失时本段不存在（与 [DIFFS] 同一处理）。这一条同时是 Phase-0 冻结基线的前提：
# 基线要求逐字节可复现，而"本机缺哪些工具"因机而异，所以环境相关的段不得进基线工作区。
TOOLS_NAMES_IN_PROJECTION = 6


def format_tools_section() -> str:
    """[TOOLS] —— 本机缺失的外部二进制（探针结果，零判断）。

    没有缺失时返回 ""：环境完整就不该占投影。

    段头自解释："这些手段在本机不可用" —— 原实现只有 `[TOOLS]` + 一坨名字，
    读者得自己推断这段意味着什么。缺工具是**本机事实**，它决定"下一跳能不能执行"，
    所以段落本身要说明这个后果。
    """
    try:
        from runtime import toolcheck
    except ImportError:
        try:
            import toolcheck  # type: ignore
        except ImportError:
            return ""

    missing = toolcheck.missing_binaries()
    if not missing:
        return ""

    shown = missing[:TOOLS_NAMES_IN_PROJECTION]
    rest = len(missing) - len(shown)
    tail = "…" if rest > 0 else ""
    return (
        f"[TOOLS] 缺 {len(missing)}/{len(toolcheck.CANDIDATE_BINARIES)} — "
        "这些手段在本机不可用（别把它们算进计划）："
        + "/".join(shown)
        + tail
        + " | 详见 python runtime/toolcheck.py\n"
        + "\n"
    )


def format_coverage_section(target: str) -> str:
    """
    COVERAGE section renderer（机械覆盖统计；零判断，只数请求次数）。

    读 responses/req_*.json 的 url，按 (scheme://netloc/path) 分组计数。
    只陈述物理量（请求数/去重路径/只探过一次的路径/单路径最大请求数），
    不推荐下一步，不判含义。无 responses 目录或无可解析 url 时返回 ""。
    """
    if not target:
        return ""
    root = os.path.join(_results_dir(), _target_identity_safe(target), "responses")
    if not os.path.isdir(root):
        return ""
    counts = {}
    total = 0
    for name in os.listdir(root):
        if not re.fullmatch(r"req_\d+\.json", name):
            continue
        try:
            with open(os.path.join(root, name), "r", encoding="utf-8") as file:
                meta = json.load(file)
        except (OSError, json.JSONDecodeError):
            continue
        url = meta.get("url")
        if not url:
            continue
        try:
            parsed = urlparse(url)
            key = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/') or '/'}"
        except ValueError:
            continue
        counts[key] = counts.get(key, 0) + 1
        total += 1
    if not counts:
        return ""
    lines = [
        "[COVERAGE]",
        f"requests: {total} | distinct_paths: {len(counts)} | "
        f"once_only_paths: {sum(1 for c in counts.values() if c == 1)}（仅 1 次请求，无差分） | "
        f"max_requests_on_single_path: {max(counts.values())}",
        "",
    ]
    return "\n".join(lines) + "\n"


def format_diffs_section(target: str) -> str:
    """
    DIFFS section renderer（层 2：机械差分检测全量投影）。

    委托 runtime/diff_scan.py 的 scan_diffs/format_diffs：按 (method, url) 分组，
    同组状态码 ≥2 种或体长差 ≥2 倍 → 候选差分。只陈述物理量差异，不判含义。
    diff_scan 不可用或无可选差分时返回 ""（不引入 fallback 判断）。
    """
    if not target:
        return ""
    try:
        from runtime.diff_scan import format_diffs as diff_format
        from runtime.diff_scan import scan_diffs
    except ImportError:
        return ""
    try:
        diffs = scan_diffs(_results_dir(), _target_identity_safe(target))
    except (OSError, ValueError):
        return ""
    return diff_format(diffs) if diffs else ""


def _compose_sections(view: WorldView, graph=None) -> list:
    """
    compile() 的段装配器（冻结顺序）。返回 [(段名, 文本), ...]，保证段名唯一。

    GOAL → TERRAIN → FINDINGS → NEXT → FRONTIER → SWEEP → EXCLUDED → WORLD MODEL
          → UNEXPLAINED → TOOLS → COVERAGE → DIFFS → RECENT EXPERIMENTS
          → OPEN EXPERIMENTS → SETTLED → ARTIFACTS.

    顺序即注意力顺序（规格 §1.2 J5 / §6，2026-09-18 按投影审计重排）：
    **去哪 → 形状 → 拿到了什么 → 现在做什么 → 待办/可派细节 → 别碰什么 → 背景 → 历史**。

    重排的两条依据（都来自真实投影实测，不是风格偏好）：
    1. `[NEXT]` 进决策区最前：投影原先只描述世界、不回答"我该做什么"，
       读者得自己推导 —— 那正是弱模型最容易失手的一跳。现在降到"抄一行"。
    2. `[RESUME]` 删除、`[RECENT EXPERIMENTS]` / `[EXCLUDED]` / `[SETTLED]` /
       `[UNEXPLAINED]` / `[TOOLS]` 正文压缩：它们原先合计占 60%+ 篇幅，
       而其中大半是与别段重复、或与"下一步"无关的过程描述。详见各段 docstring。

    段名不是装饰：它是预算裁剪的键（_enforce_budget / SECTION_TRIM_ORDER），
    所以每段必须自带名字，不能靠解析文本反推。
    """
    sections: list = []

    unexplained = unexplained_observations(graph) if graph is not None else ()

    # [GOAL] 永远在最前，且未声明时也要出现（给写法）：它是唯一的方向性信息，
    # 其余段回答"怎么走"，只有它回答"去哪"。
    goal_section = format_goal_section(view)
    if goal_section:
        sections.append(("GOAL", goal_section))

    if graph is not None:
        terrain_section = format_terrain_section(graph)
        if terrain_section:
            sections.append(("TERRAIN", terrain_section))

    # [FINDINGS] 紧跟形状之后：[GOAL] 说要什么，它说拿到了什么 —— 这对读数是唯一的进度感。
    findings_section = format_findings_section(view)
    if findings_section:
        sections.append(("FINDINGS", findings_section))

    # [NEXT] 排在决策区最前："现在做什么"是投影里最强的行动信号，
    # 它把 [FRONTIER]/[SWEEP] 的机械序变成可照抄的调用（详见 format_next_section）。
    next_section = format_next_section(view)
    if next_section:
        sections.append(("NEXT", next_section))

    for name, text in (
        ("FRONTIER", format_frontier_section(view)),
        ("SWEEP", format_sweep_section(view)),
        ("EXCLUDED", format_excluded_section(view)),
        ("WORLD MODEL", format_world_section(view)),
        ("UNEXPLAINED", format_unexplained_section(unexplained)),
    ):
        if text:
            sections.append((name, text))

    # 本机环境事实（不依赖 target）：缺失的工具决定"哪些方向在本机执行不了"。
    tools_section = format_tools_section()
    if tools_section:
        sections.append(("TOOLS", tools_section))

    if view.target:
        for name, text in (
            ("COVERAGE", format_coverage_section(view.target)),
            ("DIFFS", format_diffs_section(view.target)),
            ("RECENT EXPERIMENTS", _format_recent_experiments(view.target)),
            # v0.3.2: OPEN EXPERIMENTS 状态投影（纯展示，不引导）
            ("OPEN EXPERIMENTS", _format_open_experiments(view.target)),
        ):
            if text:
                sections.append((name, text))

    settled = format_settled_section(view)
    if settled:
        sections.append(("SETTLED", settled))

    # [RESUME] 已删除（投影审计 2026-09-18）：它逐字重复
    # `target:`（[GOAL] 段头已有）+ objects/constraints/relations/counters 计数（[TERRAIN] 已有），
    # 实测 102 字符纯冗余，且让读者怀疑两处计数是否一致。

    if view.target:
        safe_name = _target_safe_name(view.target)
        report_path = os.path.join(_results_dir(), safe_name, "report.md")
        lessons_path = os.path.join(_results_dir(), safe_name, "lessons.md")
        # 段头自解释（与本轮其余段同一原则）：原实现只有 `[ARTIFACTS]` + 两行 present/absent，
        # 读者要自己推断"这段是干嘛的、我该做什么"。
        #
        # 顺带把**收口时刻的 Read 指针**放在这里：报告的格式契约（每步附可回放的原始请求包、
        # 一洞一节、证据绑定）实测占 CLAUDE.md 15% 的每轮负载，却只在写报告那一刻有用 ——
        # 所以正文外移到 knowledge/report_spec.md，指针同时留在两处：
        #   CLAUDE.md（持久契约）+ 本段（此刻的触发点，正好显示 report.md: absent）。
        # 代价 ~55 字符，且 ARTIFACTS 从不进 SECTION_TRIM_ORDER（判据段，永不被裁）。
        artifacts = (
            "[ARTIFACTS] — 收口产物；写前先 Read knowledge/report_spec.md（格式）"
            "与 knowledge/bounty_criteria.md（过滤）\n"
            f"report.md: {'present' if os.path.exists(report_path) else 'absent'}\n"
            f"lessons.md: {'present' if os.path.exists(lessons_path) else 'absent'}\n"
            "\n"
        )
        sections.append(("ARTIFACTS", artifacts))

    return sections


def format_scope_section(target_body: str) -> str:
    """[SCOPE] —— 授权/立足点/凭据目录（2026-10-06 P1-b，仅 engagement 模式渲染）。

    条件渲染：results/<target>/scope.json 不存在 → 空串（公网单站零 IO 外的唯一额外 stat
    由调用方在已知 target 后做一次；无文件即零输出，公网投影逐字节不变）。
    永不进 SECTION_TRIM_ORDER（授权行被截断是危险的）；稳态大小 ≤400 字符，
    凭据只给计数 + list 指针（id 列表随凭据数增长，不可常驻）。
    纯 Structural Derivation：只读 scope.json 与 credentials/index.json 的机械字段。
    """
    if not target_body:
        return ""
    try:
        from scope import load_scope, creds_count  # type: ignore
    except ImportError:
        return ""
    target_id = target_identity(target_body)
    scope_data = None
    try:
        scope_data = load_scope(target_id) or load_scope(target_body.strip())
    except Exception:
        return ""
    if not scope_data:
        return ""
    allow = ",".join(scope_data.get("allow", [])[:16])
    deny = ",".join(scope_data.get("deny", [])[:16]) or "无"
    pos = (scope_data.get("position") or {}).get("at", "本机")
    try:
        n_creds = creds_count(target_id)
    except Exception:
        n_creds = 0
    eng = scope_data.get("engagement") or target_id
    lines = [
        f"[SCOPE] {eng}",
        f"  授权 {allow} · 排除 {deny} · 出网 默认禁",
        f"  立足点 {pos} · 凭据 {n_creds} 组（credentials.py list 看注，用时 Read）",
    ]
    # 量化地形锚点（Structural Derivation）：授权容量 + 最新 sha 校验过的 host_map 存活数。
    # 「未探≠不存在」由硬数字承载，不靠 LLM 记忆（防"扫了 80 = 扫完网段"）。
    terrain_line = _scope_terrain_line(target_id, scope_data)
    if terrain_line:
        lines.append(terrain_line)
    return "\n".join(lines) + "\n"


def _scope_terrain_line(target_id: str, scope_data: dict) -> str:
    """授权 CIDR 容量 + host_map 存活主机/端口观测。无法机械计算时返回空串。"""
    import ipaddress
    capacity = 0
    for raw in scope_data.get("allow", []):
        try:
            if "/" in raw:
                net = ipaddress.ip_network(raw, strict=False)
                if net.version == 4 and net.prefixlen <= 30:
                    capacity += net.num_addresses
                else:
                    capacity += 1
            elif re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", raw):
                capacity += 1
        except ValueError:
            continue  # 主机名条目不计容量
    try:
        import intranet_scan  # type: ignore
    except ImportError:
        return ""
    host_map_doc = intranet_scan.latest_verified_host_map(target_id)
    if not host_map_doc:
        return f"  授权容量 {capacity} · 未跑地形聚合（intranet_scan.py {target_id}）——未探≠不存在" if capacity else ""
    hosts = host_map_doc.get("hosts", {})
    alive = len(hosts)
    ports = sum(len(e.get("ports", [])) for e in hosts.values())
    pct = f"{alive / capacity * 100:.1f}%" if capacity else "?"
    return (f"  授权容量 {capacity} · 存活 {alive}（{pct}）· 开放端口 {ports}"
            f"（明细 intranet_scan.py）——未探≠不存在")


def compile() -> str:
    """
    Projection Renderer: emit text equivalent to former session_start stdout.

    No parameters (H0). No host chat streams (H8). No new semantics (H9).
    Single memory extract: world.parse only.

    只投影**动态世界状态**：静态宪法（认知模型 / 反压缩）已搬到 CLAUDE.md，
    由宿主随 system prompt 注入，不再占用 hook 的注入预算（见 PROJECTION_BUDGET_CHARS）。

    v0.3.2: 追加 RECENT EXPERIMENTS 和 OPEN EXPERIMENTS 状态投影。
    v0.4（2026-10-06）：engagement 模式条件渲染 [SCOPE]（GOAL 之后、TERRAIN 之前）。
    """
    lines = _read_memory_lines()
    # 一次读取，两个视图：world 提供桶（原渲染器不动），graph 提供 kind/state 计数与锚点查询。
    # 两者对同一批 memory 的划分由 tests/test_graph_roundtrip.py 锁定一致。
    view = world_parse(lines)
    graph = graph_parse(lines)

    sections = _compose_sections(view, graph)
    # [SCOPE] 条件插入 GOAL 之后（不进 _compose_sections 冻结序，避免公网路径多一次 stat 签名变更；
    # 段名仍自带，永不裁——它不在 SECTION_TRIM_ORDER）
    scope_section = format_scope_section(view.target)
    if scope_section:
        out = []
        inserted = False
        for name, text in sections:
            out.append((name, text))
            if name == "GOAL" and not inserted:
                out.append(("SCOPE", scope_section))
                inserted = True
        if not inserted:
            out.insert(0, ("SCOPE", scope_section))
        sections = out

    sections = _enforce_budget(sections, PROJECTION_BUDGET_CHARS)
    return "".join(text for _, text in sections)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.stdout.write(compile())
    except Exception as e:
        print(f"[project.compile] ERROR: {str(e)[:200]}")
    sys.exit(0)


if __name__ == "__main__":
    main()
