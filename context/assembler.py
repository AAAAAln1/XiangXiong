#!/usr/bin/env python3
"""
assembler.py — 纯机械上下文组装器 (Experiment F 验证阶段)。

职责（仅机械加载，零判断）：
- 读取 memory.md → world model（objects/relations/constraints/counters）
- 读取 experiments/ → 最近 N 个 observation（纯时间窗口，不做语义筛选）
- 读取 CLAUDE.md + 核心 cognition → contract（全量，不裁剪）

禁止（任何含判断/排序/筛选/建议的操作）：
- 不判断 evidence 重要性
- 不排序 relation / hypothesis
- 不生成 priority / next_action / strategy / possible_experiments
- 不筛选「改变世界状态的 observation」
- 不按 status/closed 过滤 experiment
- 不生成任何建议或步骤

原则：
- Assembler 决定模型看见什么世界 → 只机械加载
- LLM 决定在世界中采取什么行动 → 不由 Assembler 干预
- Runtime 只负责执行和记账 → 不在此模块
"""

import json
import os
import sys
from datetime import datetime, timezone

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_PATH = os.path.join(_PROJECT_ROOT, ".claude", "memory.md")
RESULTS_DIR = os.path.join(_PROJECT_ROOT, "results")
CLAUDE_PATH = os.path.join(_PROJECT_ROOT, "CLAUDE.md")

# Contract 源：CLAUDE.md 全文 + 核心 cognition 文件
COGNITION_CONTRACT_FILES = (
    "object_model.md",
    "evidence_model.md",
)

# Recent Observation 窗口大小（纯时间窗口，最近 N 个 experiment）
RECENT_EXPERIMENT_LIMIT = 5

# 输出截断
MAX_CONTRACT_CHARS = 3000
MAX_OBSERVATION_CHARS = 200


def safe_target(target: str) -> str:
    return target.replace(":", "_").replace("/", "_").replace("\\", "_")


def read_memory() -> list:
    """读取 memory.md 全部行。"""
    if not os.path.exists(MEMORY_PATH):
        return []
    with open(MEMORY_PATH, "r", encoding="utf-8") as f:
        return f.readlines()


def extract_world_model(lines: list) -> dict:
    """
    从 memory.md 提取世界模型（纯前缀分类，零语义判断）。

    分类规则（与 session_start.py 一致）：
    - surface: / cred: → objects
    - vuln: → objects（同时记录状态来源）
    - tech: / port: / artifact: → constraints
    - hypothesis: / hypothesis [state]: → relations（生命状态来自 store）
    - counter: / dead: / dead-summary: → counters
    - target: → target identity
    - future: 不投影

    状态标记：hypothesis 行可选 [state] 前缀，值来自 merge/world store
    - [observed]：已观察到但未验证
    - [confirmed]：已验证为真
    - [contradicted]：已验证为假
    - [unknown]：无法确定
    """
    model = {
        "target": "",
        "objects": [],
        "relations_with_state": [],  # (line_text, state) tuples
        "constraints": [],
        "counters_raw": [],
        "deads_raw": [],
    }
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("target:"):
            model["target"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("surface:") or stripped.startswith("cred:"):
            model["objects"].append(stripped)
        elif stripped.startswith("vuln:"):
            model["objects"].append(stripped)
        elif stripped.startswith("tech:") or stripped.startswith("port:") or stripped.startswith("artifact:"):
            model["constraints"].append(stripped)
        elif stripped.startswith("counter:"):
            model["counters_raw"].append(stripped)
        elif stripped.startswith("dead:") or stripped.startswith("dead-summary:"):
            model["deads_raw"].append(stripped)
        elif stripped.startswith("hypothesis"):
            # 解析 hypothesis [state]: content 格式
            state = "unverified"  # 默认状态
            rest = stripped
            # 格式1: hypothesis [state]: content
            if "[" in rest and "]" in rest:
                bracket_open = rest.index("[")
                bracket_close = rest.index("]")
                state_candidate = rest[bracket_open+1:bracket_close].strip().lower()
                if state_candidate in ("observed", "confirmed", "contradicted", "unknown"):
                    state = state_candidate
                # 移除状态标记部分用于显示（同时去除多余空格）
                before = rest[:bracket_open].rstrip()
                after = rest[bracket_close+1:].lstrip()
                rest = before + after
            model["relations_with_state"].append((rest, state))

    return model


def load_recent_experiments(target: str, limit: int = RECENT_EXPERIMENT_LIMIT) -> list:
    """
    加载最近 N 个 experiment 的最新 observation。

    纯机械规则：
    - 按 experiment_id 自然排序（exp-001 < exp-002 < ...），取最后 N 个
    - 不筛选 closed/open
    - 不筛选「改变世界状态的」
    - 不按 importance/priority 排序
    """
    root = os.path.join(RESULTS_DIR, safe_target(target), "experiments")
    if not os.path.isdir(root):
        return []

    experiment_ids = sorted(
        name for name in os.listdir(root)
        if name.startswith("exp-") and os.path.isdir(os.path.join(root, name))
    )
    # 纯时间窗口：取最后 N 个
    recent_ids = experiment_ids[-limit:]

    experiments = []
    for experiment_id in recent_ids:
        path = os.path.join(root, experiment_id, "experiment.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        experiments.append(_summarize_experiment(experiment_id, metadata))
    return experiments


def _summarize_experiment(experiment_id: str, metadata: dict) -> dict:
    """机械提取 experiment 摘要。不判断重要性。"""
    summary = {
        "id": experiment_id,
        "created_at": metadata.get("created_at", ""),
        "hypothesis": metadata.get("hypothesis", ""),
        "status": metadata.get("status", "unknown"),
        "closed": metadata.get("closed", False),
        "last_observation": "",
        "conclusion": "",
    }
    observations = metadata.get("observations") or []
    if observations and isinstance(observations[-1], dict):
        obs_text = observations[-1].get("observation", "")
        summary["last_observation"] = obs_text[:MAX_OBSERVATION_CHARS]
    if metadata.get("conclusion"):
        summary["conclusion"] = metadata["conclusion"][:MAX_OBSERVATION_CHARS]
    return summary


def load_contract() -> str:
    """机械加载合约：CLAUDE.md + 核心 cognition 文件。不做语义裁剪。"""
    parts = []

    # CLAUDE.md 约束段
    if os.path.exists(CLAUDE_PATH):
        with open(CLAUDE_PATH, "r", encoding="utf-8") as f:
            claude_text = f.read()
        parts.append(claude_text)

    # 核心 cognition 文件
    cognition_dir = os.path.join(_PROJECT_ROOT, "cognition")
    for name in COGNITION_CONTRACT_FILES:
        path = os.path.join(cognition_dir, name)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
                parts.append(f"--- cognition/{name} ---\n{text}")
            except (OSError, UnicodeError):
                pass

    full = "\n\n".join(parts)
    if len(full) > MAX_CONTRACT_CHARS:
        full = full[:MAX_CONTRACT_CHARS] + "\n...[contract truncated]"
    return full


def shorten(text: str, limit: int = 200) -> str:
    """截断文本。"""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "..."


def render_world(model: dict) -> str:
    """渲染 [WORLD] 段。全量投影，不筛选不排序。"""
    lines = ["[WORLD]"]
    if model["target"]:
        lines.append(f"target: {model['target']}")
    else:
        lines.append("target: (none)")
    lines.append("")

    lines.append(f"objects ({len(model['objects'])}):")
    for obj in model["objects"]:
        lines.append(f"  {obj}")
    lines.append("")

    lines.append(f"relations ({len(model['relations_with_state'])}):")
    for rel, state in model["relations_with_state"]:
        if state and state != "unverified":
            lines.append(f"  {rel}  [{state}]")
        else:
            lines.append(f"  {rel}")
    lines.append("")

    lines.append(f"constraints ({len(model['constraints'])}):")
    for c in model["constraints"]:
        lines.append(f"  {c}")
    lines.append("")

    counters = model["counters_raw"] + model["deads_raw"]
    if counters:
        lines.append(f"counters ({len(counters)}):")
        for counter in counters:
            lines.append(f"  {counter}")
        lines.append("")

    return "\n".join(lines)


def render_recent(experiments: list) -> str:
    """渲染 [RECENT OBSERVATION] 段。纯时间窗口，不筛选不排序。"""
    if not experiments:
        return "[RECENT OBSERVATION]\n(none)\n"

    lines = ["[RECENT OBSERVATION]"]
    for exp in experiments:
        fields = []
        fields.append(f"id: {exp['id']}")
        fields.append(f"closed: {str(exp['closed']).lower()}")
        if exp["hypothesis"]:
            fields.append(f"hypothesis: {shorten(exp['hypothesis'])}")
        if exp["last_observation"]:
            fields.append(f"observation: {shorten(exp['last_observation'])}")
        if exp["conclusion"]:
            fields.append(f"conclusion: {shorten(exp['conclusion'])}")
        fields.append(f"status: {exp['status']}")
        lines.append("  " + " | ".join(fields))
    lines.append("")
    return "\n".join(lines)


def render_contract() -> str:
    """渲染 [CONTRACT] 段。全量加载，不裁剪。"""
    contract_text = load_contract()
    lines = ["[CONTRACT]"]
    lines.append(contract_text)
    lines.append("")
    return "\n".join(lines)


# ── 主入口 ──

def assemble(target: str = None) -> str:
    """
    组装模型上下文。

    Args:
        target: 目标标识。为 None 时从 memory.md 读取。

    Returns:
        格式化的 [WORLD] + [RECENT OBSERVATION] + [CONTRACT] 文本。
    """
    memory_lines = read_memory()
    model = extract_world_model(memory_lines)

    resolved_target = target or model["target"]
    if not resolved_target:
        return "[ERROR] no target in memory.md and no target argument\n"

    recent = load_recent_experiments(resolved_target)

    parts = [
        render_world(model),
        f"\n{render_recent(recent)}",
        f"\n{render_contract()}",
    ]
    return "\n".join(parts)


# ── CLI（供手动验证和 SessionStart 调用） ──

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    target = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        output = assemble(target)
        print(output)
    except Exception as e:
        print(f"[assembler] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
