#!/usr/bin/env python3
"""
session_start.py — SessionStart hook 脚本。

职责（纯 IO，零决策）：
1. 读取 .observe_state.json，投影 observe 指标
2. 读取 memory.md，投影 [WORLD MODEL] [RESUME]
3. 读取当前目标最近实验，投影 [RECENT EXPERIMENTS]

输出段（stdout，被 Claude Code 注入 Agent 上下文）：
  [COGNITION] 固定 cognition 文件
  [ANTI-COMPRESSION] 极短镜头（固定 6 行：4 防错误压缩 + 2 支持后行动约束；不读 false_closures.md 正文）
  [OBSERVE] consecutive_none / same_action_count / stagnation_level / last_round
  [WORLD MODEL] 对象/约束/反证优先，未知殿后；实验段在未知前（attention 顺序，非 schema）
  [RECENT EXPERIMENTS] 当前目标最近实验事实（在 unknowns 前投影）
  [RESUME] 恢复上下文投影资格（有 target 且 world 桶非空时出现；非「继续/停止」判决）
  [ARTIFACTS] report.md / lessons.md 磁盘存在性（元数据 only；不参与 RESUME 布尔）

原则：
- 只读，不写任何文件
- 不判断「该不该停」「该不该换方向」——那是 Agent 的事
- [RESUME] = 是否投影恢复上下文，不是 Continue Decision；只陈述计数等事实，无指令
- 产物存在 ≠ 探索完成；report 不得关闭 [RESUME]
- [ANTI-COMPRESSION] = exposure（认知接口可达），不是 trigger/Router/漏洞菜；无动作建议
"""

import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_PATH = os.path.join(_PROJECT_ROOT, ".claude", "memory.md")
STATE_PATH = os.path.join(_PROJECT_ROOT, ".observe_state.json")
RESULTS_DIR = os.path.join(_PROJECT_ROOT, "results")
COGNITION_MODULES = (
    "object_model.md",
    "evidence_model.md",
    "experiment_model.md",
    "surface_misalignment.md",
)
MAX_COGNITION_FILE_CHARS = 4000
MAX_COGNITION_TOTAL_CHARS = 12000

# Minimal compression-discipline exposure (not false_closures.md dump; not a trigger).
# 4 lines: don't unjustifiably compress the world.
# 2 lines: don't keep irrelevant unknowns equally active after support.
# Not vuln count; not planner; not "False Openness" ontology.
ANTI_COMPRESSION_LINES = (
    "Similar identifier ≠ same object.",
    "Related behavior ≠ proven shared boundary.",
    "Successful path ≠ adjacent surface exhausted.",
    "Missing observation ≠ absence.",
    "Supported explanations should constrain the next experiment.",
    "Preserved unknowns do not retain equal priority outside the current explanation boundary.",
)


def render_anti_compression() -> str:
    """Fixed short lens only: no file load, no action advice, no FC body."""
    parts = ["[ANTI-COMPRESSION]"]
    for line in ANTI_COMPRESSION_LINES:
        parts.append(f"- {line}")
    return "\n".join(parts) + "\n"


def load_cognition_modules(root: str) -> list:
    modules = []
    total = 0
    cognition_dir = os.path.join(root, "cognition")
    for name in COGNITION_MODULES:
        path = os.path.join(cognition_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as file:
                content = file.read(MAX_COGNITION_FILE_CHARS + 1)
            content = content[:MAX_COGNITION_FILE_CHARS]
            remaining = MAX_COGNITION_TOTAL_CHARS - total
            content = content[:max(0, remaining)]
            modules.append((name, content))
            total += len(content)
        except FileNotFoundError:
            modules.append((name, f"missing: cognition/{name}"))
        except (OSError, UnicodeError) as error:
            modules.append((name, f"error: cognition/{name}: {type(error).__name__}"))
    return modules


def render_cognition(modules: list) -> str:
    parts = ["[COGNITION]"]
    for name, content in modules:
        parts.append(f"--- cognition/{name} ---")
        parts.append(content)
    return "\n".join(parts) + "\n"


def load_observe_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version", 0) != 3:
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def compute_level(consecutive_none: int, same_count: int) -> str:
    if consecutive_none >= 15 and same_count >= 10:
        return "emergency"
    if consecutive_none >= 9 or same_count >= 7:
        return "critical"
    if consecutive_none >= 5 or same_count >= 4:
        return "warning"
    return "normal"


def target_safe_name(target: str) -> str:
    return target.replace(":", "_").replace("/", "_").replace("\\", "_")


def load_recent_experiments(target: str, limit: int = 5) -> list:
    root = os.path.join(RESULTS_DIR, target_safe_name(target), "experiments")
    if not os.path.isdir(root):
        return []

    experiment_ids = sorted(
        name for name in os.listdir(root)
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
            (item for item in experiments if item.get("experiment_id", item["_sort_id"]) == candidate),
            None,
        )
        if active is not None and not active.get("closed"):
            active_id = candidate
            active = dict(active, active=True)
    except OSError:
        active = None
    others = [
        item for item in experiments
        if item.get("experiment_id", item["_sort_id"]) != active_id
    ]
    if active is not None and active_id is not None:
        return [active] + others[-max(0, limit - 1):]
    return others[-limit:]


def shorten(value, limit: int = 240) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "..."


def print_recent_experiments(target: str) -> None:
    experiments = load_recent_experiments(target)
    if not experiments:
        return
    print("[RECENT EXPERIMENTS]")
    for metadata in experiments:
        fields = []
        experiment_id = metadata.get("experiment_id") or metadata.get("_sort_id")
        if experiment_id:
            fields.append(f"id: {experiment_id}")
        if metadata.get("active"):
            fields.append("active: true")
        fields.append(f"closed: {str(bool(metadata.get('closed'))).lower()}")
        if metadata.get("hypothesis"):
            fields.append(f"hypothesis: {shorten(metadata['hypothesis'])}")
        if metadata.get("action"):
            fields.append(f"action: {shorten(metadata['action'])}")
        observations = metadata.get("observations") or []
        if observations and isinstance(observations[-1], dict):
            last_observation = observations[-1]
            if last_observation.get("observation"):
                fields.append(f"observation: {shorten(last_observation['observation'])}")
            if last_observation.get("artifact"):
                fields.append(f"artifact: {last_observation['artifact']}")
        if metadata.get("conclusion"):
            fields.append(f"conclusion: {shorten(metadata['conclusion'])}")
        if metadata.get("closed"):
            fields.append(f"evidence: experiments/{experiment_id}/evidence.md")
        if metadata.get("status"):
            fields.append(f"status: {metadata['status']}")
        if fields:
            print("  " + " | ".join(fields))
    print()


def should_resume(model: dict) -> bool:
    """Resume projection eligibility: target + any world-bucket content.

    Not a stop/continue decision. report.md must not gate this.
    """
    has_state = any((model["objects"], model["constraints"], model["unknowns"], model["counters"]))
    return bool(model["target"] and has_state)


def extract_world_model(lines: list) -> dict:
    """
    从 memory.md 提取世界模型（纯前缀分类，零语义判断）。

    分类规则：
    - surface: / vuln: / cred: → objects
    - tech: / port: / artifact: → constraints
    - hypothesis: → unknowns（未解决假设，可选状态标记 [observed|confirmed|contradicted|unknown]）
    - counter: / dead: / dead-summary: → counters
    - target: → target identity
    - future: 不投影
    """
    model = {
        "target": "",
        "objects": [],
        "unknowns_with_state": [],  # 改为带状态的 tuple list
        "constraints": [],
        "counters": [],
    }
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("target:"):
            model["target"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("surface:") or stripped.startswith("vuln:") or stripped.startswith("cred:"):
            model["objects"].append(stripped)
        elif stripped.startswith("tech:") or stripped.startswith("port:") or stripped.startswith("artifact:"):
            model["constraints"].append(stripped)
        elif stripped.startswith("hypothesis"):
            # 解析 hypothesis [state]: content 格式
            state = None  # 默认无状态标记
            rest = stripped
            if "[" in rest and "]" in rest:
                bracket_open = rest.index("[")
                bracket_close = rest.index("]")
                state_candidate = rest[bracket_open+1:bracket_close].strip().lower()
                if state_candidate in ("observed", "confirmed", "contradicted", "unknown"):
                    state = state_candidate
                    # 移除状态标记部分用于显示
                    before = rest[:bracket_open].rstrip()
                    after = rest[bracket_close+1:].lstrip()
                    rest = before + after
            model["unknowns_with_state"].append((rest, state))
        elif stripped.startswith("counter:") or stripped.startswith("dead:") or stripped.startswith("dead-summary:"):
            model["counters"].append(stripped)
        # future: 不投影
    return model


def main():
    print(render_cognition(load_cognition_modules(_PROJECT_ROOT)))
    print(render_anti_compression())

    # ── 1. observe 指标 ──
    state = load_observe_state()
    cn = state.get("consecutive_none", 0)
    last_round = state.get("last_round", 0)

    actions = state.get("actions", [])
    same_count = 0
    if actions:
        last_fp = actions[-1]["fp"]
        for a in reversed(actions):
            if a["fp"] == last_fp:
                same_count += 1
            else:
                break

    level = compute_level(cn, same_count)

    # ── 2. 读取 memory ──
    memory_lines = []
    if os.path.exists(MEMORY_PATH):
        with open(MEMORY_PATH, "r", encoding="utf-8") as f:
            memory_lines = f.readlines()

    # ── 3. OBSERVE 段 ──
    print("[OBSERVE]")
    print(f"consecutive_none: {cn}")
    print(f"same_action_count: {same_count}")
    print(f"stagnation_level: {level}")
    print(f"last_round: {last_round}")
    print()

    # ── 4. WORLD MODEL 段（对象中心世界模型；attention 顺序，非 schema）──
    # objects/constraints/counters first → recent experiments → unknowns last.
    # 解释与证据先于未知列表，避免 unknown=todo 默认权重。
    model = extract_world_model(memory_lines)
    print("[WORLD MODEL]")
    if model["target"]:
        print(f"target: {model['target']}")
    else:
        print("target: (none)")
    print()

    print(f"objects ({len(model['objects'])}):")
    for obj in model["objects"]:
        print(f"  {obj}")
    print()

    print(f"constraints ({len(model['constraints'])}):")
    for c in model["constraints"]:
        print(f"  {c}")
    print()

    print(f"counters ({len(model['counters'])}):")
    for counter in model["counters"]:
        print(f"  {counter}")
    print()

    # ── 5. 最近实验事实投影（在 unknowns 前；解释→实验，非 unknown 优先）──
    if model["target"]:
        print_recent_experiments(model["target"])

    # 未知殿后（防单行无限膨胀的世界；读 memory.md 时已硬截）
    if model["unknowns_with_state"]:
        print("以下关系来自当前世界模型。")
        print("它们不是任务要求，也不是下一步指令。")
        print("它们表示当前已形成但仍可能影响判断的世界解释。")
        print()
        print(f"relations ({len(model['unknowns_with_state'])}):")
        for u, state in model["unknowns_with_state"]:
            if state:
                print(f"  {u}  [{state}]")
            else:
                print(f"  {u}")
        print()

    # ── 6. RESUME 段（恢复上下文投影资格；非停止判决）──
    counters_count = len(model["counters"])
    has_state = any((model["objects"], model["constraints"], model["unknowns_with_state"], model["counters"]))
    if model["target"] and has_state:
        print("[RESUME]")
        print(f"target: {model['target']}")
        print(
            f"objects: {len(model['objects'])} | "
            f"constraints: {len(model['constraints'])} | "
            f"relations: {len(model['unknowns_with_state'])} | counters: {counters_count}"
        )
        print()

    # ── 7. ARTIFACTS 段（磁盘元数据 only；与 world/resume 并列，不关 RESUME）──
    if model["target"]:
        safe_name = target_safe_name(model["target"])
        report_path = os.path.join(RESULTS_DIR, safe_name, "report.md")
        lessons_path = os.path.join(RESULTS_DIR, safe_name, "lessons.md")
        print("[ARTIFACTS]")
        print(f"report.md: {'present' if os.path.exists(report_path) else 'absent'}")
        print(f"lessons.md: {'present' if os.path.exists(lessons_path) else 'absent'}")
        print()

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[session_start] ERROR: {str(e)[:200]}")
        sys.exit(0)
