#!/usr/bin/env python3
"""
project.py — Phase 1.3 Projection Renderer (H0–H15).

compile() is a stable generator of the former session_start stdout.
Ownership move only: no new projection semantics, no parameters.
Does not read host chat streams as projection input.

Memory structure comes only from world.parse() (single extract source).
OPEN/STALLED and related display rules live only in this renderer (H9 legacy).

Naming: Projection Renderer / Context Projection.
"""

from __future__ import annotations

import json
import os
import sys

try:
    from .paths import project_root, world_active
    from .world import ParsedLine, WorldView, parse as world_parse
except ImportError:
    from paths import project_root, world_active
    from world import ParsedLine, WorldView, parse as world_parse

# Fixed list and order — must match former session_start (H10).
COGNITION_MODULES = (
    "object_model.md",
    "evidence_model.md",
    "experiment_model.md",
    "surface_misalignment.md",
)
MAX_COGNITION_FILE_CHARS = 4000
MAX_COGNITION_TOTAL_CHARS = 12000

# Frozen legacy anti-compression lines (Phase0 / old session_start).
ANTI_COMPRESSION_LINES = (
    "Similar identifier ≠ same object.",
    "Related behavior ≠ proven shared boundary.",
    "Successful path ≠ adjacent surface exhausted.",
    "Missing observation ≠ absence.",
    "Supported explanations should constrain the next experiment.",
    "Preserved unknowns do not retain equal priority outside the current explanation boundary.",
)


def _results_dir() -> str:
    return os.path.join(project_root(), "results")


def _render_anti_compression() -> str:
    parts = ["[ANTI-COMPRESSION]"]
    for line in ANTI_COMPRESSION_LINES:
        parts.append(f"- {line}")
    return "\n".join(parts) + "\n"


def _load_cognition_modules(root: str) -> list:
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
            content = content[: max(0, remaining)]
            modules.append((name, content))
            total += len(content)
        except FileNotFoundError:
            modules.append((name, f"missing: cognition/{name}"))
        except (OSError, UnicodeError) as error:
            modules.append((name, f"error: cognition/{name}: {type(error).__name__}"))
    return modules


def _render_cognition(modules: list) -> str:
    parts = ["[COGNITION]"]
    for name, content in modules:
        parts.append(f"--- cognition/{name} ---")
        parts.append(content)
    return "\n".join(parts) + "\n"


def _target_safe_name(target: str) -> str:
    return target.replace(":", "_").replace("/", "_").replace("\\", "_")


def _load_recent_experiments(target: str, limit: int = 5) -> list:
    # target may be full description; extract pure domain
    from merge import target_identity
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
    """Former print_recent_experiments body as string (H9)."""
    if not target:
        return ""
    
    experiments = _load_recent_experiments(target)
    if not experiments:
        return ""
    lines = ["[RECENT EXPERIMENTS]"]
    for metadata in experiments:
        fields = []
        experiment_id = metadata.get("experiment_id") or metadata.get("_sort_id")
        if experiment_id:
            fields.append(f"id: {experiment_id}")
        if metadata.get("active"):
            fields.append("active: true")
        fields.append(f"closed: {str(bool(metadata.get('closed'))).lower()}")
        if metadata.get("hypothesis"):
            fields.append(f"hypothesis: {_shorten(metadata['hypothesis'])}")
        if metadata.get("action"):
            fields.append(f"action: {_shorten(metadata['action'])}")
        observations = metadata.get("observations") or []
        if observations and isinstance(observations[-1], dict):
            last_observation = observations[-1]
            if last_observation.get("observation"):
                fields.append(f"observation: {_shorten(last_observation['observation'])}")
            if last_observation.get("artifact"):
                fields.append(f"artifact: {last_observation['artifact']}")
        if metadata.get("conclusion"):
            fields.append(f"conclusion: {_shorten(metadata['conclusion'])}")
        if metadata.get("closed"):
            fields.append(f"evidence: experiments/{experiment_id}/evidence.md")
        if metadata.get("status"):
            fields.append(f"status: {metadata['status']}")
        if fields:
            lines.append("  " + " | ".join(fields))
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
        conclusion = os.path.join(exp_dir, "conclusion.md")
        
        # 只展示未关闭（有 json 无 conclusion）
        if not os.path.exists(exp_json) or os.path.exists(conclusion):
            continue
        
        try:
            with open(exp_json, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        except (OSError, json.JSONDecodeError):
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
        return f.readlines()


def _hypothesis_display_text(item: ParsedLine) -> str:
    """
    Legacy session_start display form for hypothesis lines (renderer only).

    Old extract stripped [state] from the line before printing, then re-appended
    [state] for non-blocked OPEN rows. Keep that presentation; raw stays in parse.
    """
    raw = item.raw
    if item.state is None:
        return raw
    if not (raw.startswith("hypothesis [") and "]" in raw):
        return raw
    close_i = raw.index("]")
    before = raw[: len("hypothesis ")].rstrip()  # "hypothesis"
    # raw is "hypothesis [state]: rest" or "hypothesis [state]rest"
    after = raw[close_i + 1 :].lstrip()
    return before + after


def _world_bucket_lists(view: WorldView) -> tuple[list[str], list[str], list[str]]:
    """Surface buckets for WORLD MODEL body (legacy SessionStart filters)."""
    world_objects = [
        item.raw
        for item in view.objects
        if item.prefix in ("surface", "vuln", "cred")
    ]
    world_constraints = [item.raw for item in view.constraints]
    world_counters = [item.raw for item in view.counters]
    return world_objects, world_constraints, world_counters


def format_world_section(view: WorldView) -> str:
    """
    World bucket section renderer (single world projection source, Phase 2.0 fix).

    Emits only the [WORLD MODEL] block (target / objects / constraints / counters).
    Does NOT emit unknowns/RECENT/RESUME/ARTIFACTS and does NOT decide ordering.
    """
    world_objects, world_constraints, world_counters = _world_bucket_lists(view)

    world_lines = ["[WORLD MODEL]"]
    if view.target:
        world_lines.append(f"target: {view.target}")
    else:
        world_lines.append("target: (none)")
    world_lines.append("")

    world_lines.append(f"objects ({len(world_objects)}):")
    for obj in world_objects:
        world_lines.append(f"  {obj}")
    world_lines.append("")

    world_lines.append(f"constraints ({len(world_constraints)}):")
    for c in world_constraints:
        world_lines.append(f"  {c}")
    world_lines.append("")

    world_lines.append(f"counters ({len(world_counters)}):")
    for counter in world_counters:
        world_lines.append(f"  {counter}")
    world_lines.append("")
    return "\n".join(world_lines) + "\n"


def format_unknown_sections(view: WorldView) -> str:
    """
    Unknowns section renderer (single world projection source, Phase 2.0 fix).

    镜像 world.py 的 unknowns bucket：全部 hypothesis 行按 memory 顺序展示，
    state 是行内属性（observed/confirmed/contradicted/blocked/unknown 全量）。
    不按 state 过滤或分组，不发明 schema 外分类词（OPEN/STALLED/CLOSED）。
    不解释 state 含义；含义由 LLM 结合上下文判断。
    """
    if not view.unknowns:
        return ""

    unknown_lines = [
        "以下关系来自当前世界模型。",
        "它们不是任务要求，也不是下一步指令。",
        "",
        f"HYPOTHESES ({len(view.unknowns)}):",
    ]
    for item in view.unknowns:
        disp = _hypothesis_display_text(item)
        suffix = f"  [{item.state}]" if item.state else ""
        unknown_lines.append(f"  {disp}{suffix}")
    unknown_lines.append("")
    return "\n".join(unknown_lines) + "\n"


def experiments_dir_for_target(target: str) -> str:
    """Shared experiment directory path (no third ad-hoc root)."""
    # target may be full description; extract pure domain
    from merge import target_identity
    target_id = target_identity(target) if target else target
    return os.path.join(_results_dir(), _target_safe_name(target_id), "experiments")


def _render_world_from_view(view: WorldView) -> str:
    """
    SessionStart compile() document composer (frozen order).

    WORLD → RECENT EXPERIMENTS → OPEN EXPERIMENTS → HYPOTHESES → RESUME → ARTIFACTS.
    Sections come from shared renderers; only this composer decides order.
    """
    parts: list[str] = [format_world_section(view)]

    if view.target:
        recent = _format_recent_experiments(view.target)
        if recent:
            parts.append(recent)
        
        # v0.3.2: OPEN EXPERIMENTS 状态投影（纯展示，不引导）
        open_exps = _format_open_experiments(view.target)
        if open_exps:
            parts.append(open_exps)

    unknowns = format_unknown_sections(view)
    if unknowns:
        parts.append(unknowns)

    world_objects, world_constraints, world_counters = _world_bucket_lists(view)
    relations_count = len(view.unknowns)
    counters_count = len(world_counters)
    has_state = any(
        (
            world_objects,
            world_constraints,
            view.unknowns,
            world_counters,
        )
    )
    if view.target and has_state:
        resume = (
            "[RESUME]\n"
            f"target: {view.target}\n"
            f"objects: {len(world_objects)} | "
            f"constraints: {len(world_constraints)} | "
            f"relations: {relations_count} | "
            f"counters: {counters_count}\n"
            "\n"
        )
        parts.append(resume)

    if view.target:
        safe_name = _target_safe_name(view.target)
        report_path = os.path.join(_results_dir(), safe_name, "report.md")
        lessons_path = os.path.join(_results_dir(), safe_name, "lessons.md")
        artifacts = (
            "[ARTIFACTS]\n"
            f"report.md: {'present' if os.path.exists(report_path) else 'absent'}\n"
            f"lessons.md: {'present' if os.path.exists(lessons_path) else 'absent'}\n"
            "\n"
        )
        parts.append(artifacts)

    return "".join(parts)


def compile() -> str:
    """
    Projection Renderer: emit text equivalent to former session_start stdout.

    No parameters (H0). No host chat streams (H8). No new semantics (H9).
    Single memory extract: world.parse only.
    
    v0.3.2: 追加 RECENT EXPERIMENTS 和 OPEN EXPERIMENTS 状态投影。
    """
    root = project_root()
    parts: list[str] = []

    # Former session_start used print(s) where s already ended with "\n",
    # so print added one extra blank line after COGNITION / ANTI-COMPRESSION.
    parts.append(_render_cognition(_load_cognition_modules(root)))
    parts.append("\n")
    parts.append(_render_anti_compression())
    parts.append("\n")

    view = world_parse(_read_memory_lines())
    parts.append(_render_world_from_view(view))

    return "".join(parts)


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
