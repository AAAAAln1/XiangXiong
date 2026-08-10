#!/usr/bin/env python3
"""目标级实验事实管理。

Runtime 只保存调用方提供的假设、动作、观察与结论，不生成或解释内容。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")
EXPERIMENT_ID_PATTERN = re.compile(r"^exp-\d{3,}$")
FINAL_STATUSES = {"success", "failed", "blocked", "unknown"}

# 实验最终状态 → memory hypothesis 行内状态（机械映射，语义由 LLM 的 --status 决定）
MEMORY_STATUS_MAP = {
    "success": "confirmed",
    "failed": "contradicted",
    "blocked": "blocked",
    "unknown": "observed",
}
VALID_MEMORY_STATES = {"confirmed", "contradicted", "blocked", "observed"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_target(target: str) -> str:
    return target.replace(":", "_").replace("/", "_").replace("\\", "_")


def experiments_dir(target: str) -> str:
    return os.path.join(RESULTS_DIR, safe_target(target), "experiments")


def next_experiment_id(target: str) -> str:
    root = experiments_dir(target)
    highest = 0
    if os.path.isdir(root):
        for name in os.listdir(root):
            match = re.match(r"^exp-(\d+)$", name)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"exp-{highest + 1:03d}"


def validate_experiment_id(experiment_id: str) -> None:
    if not EXPERIMENT_ID_PATTERN.fullmatch(experiment_id):
        raise ValueError("experiment_id 必须使用 exp-NNN 格式")


def experiment_dir(target: str, experiment_id: str) -> str:
    validate_experiment_id(experiment_id)
    return os.path.join(experiments_dir(target), experiment_id)


def metadata_path(target: str, experiment_id: str) -> str:
    return os.path.join(experiment_dir(target, experiment_id), "experiment.json")


def write_json(path: str, value: dict) -> None:
    temp_path = path + f".{os.getpid()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def active_path(target: str) -> str:
    return os.path.join(experiments_dir(target), ".active")


def activate_experiment(target: str, experiment_id: str) -> dict:
    metadata = load_experiment(target, experiment_id)
    if metadata.get("closed"):
        raise ValueError("experiment 已关闭")
    current = resolve_experiment_id(target)
    if current is not None and current != experiment_id:
        raise ValueError(f"已有 active experiment: {current}")
    root = experiments_dir(target)
    os.makedirs(root, exist_ok=True)
    path = active_path(target)
    temp_path = path + f".{os.getpid()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        file.write(experiment_id)
    os.replace(temp_path, path)
    return {"target": target, "experiment_id": experiment_id, "active": True}


def deactivate_experiment(target: str, experiment_id: str = None) -> dict:
    path = active_path(target)
    removed = False
    try:
        with open(path, "r", encoding="utf-8") as file:
            current = file.read().strip()
        if experiment_id is None or current == experiment_id:
            os.remove(path)
            removed = True
    except FileNotFoundError:
        current = None
    return {"target": target, "experiment_id": current, "active": not removed and current is not None}


def resolve_experiment_id(target: str, explicit_id: str = None):
    if explicit_id is not None:
        return explicit_id
    path = active_path(target)
    try:
        with open(path, "r", encoding="utf-8") as file:
            experiment_id = file.read().strip()
        validate_experiment_id(experiment_id)
        metadata = load_experiment(target, experiment_id)
        if metadata.get("closed"):
            raise ValueError("experiment 已关闭")
        return experiment_id
    except (OSError, ValueError, json.JSONDecodeError):
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def load_experiment(target: str, experiment_id: str) -> dict:
    path = metadata_path(target, experiment_id)
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def relative_path(target: str, path: str) -> str:
    target_root = os.path.join(RESULTS_DIR, safe_target(target))
    return os.path.relpath(path, target_root).replace("\\", "/")


def create_experiment(
    target: str,
    hypothesis: str = None,
    action: str = None,
    experiment_id: str = None,
    activate: bool = False,
    related_memory_hypothesis: str = None,
) -> dict:
    if activate and not hypothesis:
        raise ValueError("active experiment 必须提供 hypothesis")
    if activate:
        current = resolve_experiment_id(target)
        if current is not None:
            raise ValueError(f"已有 active experiment: {current}")
    experiment_id = experiment_id or next_experiment_id(target)
    path = experiment_dir(target, experiment_id)
    os.makedirs(path, exist_ok=False)

    created_at = now_iso()
    metadata = {
        "experiment_id": experiment_id,
        "target": target,
        "created_at": created_at,
        "closed_at": None,
        "hypothesis": hypothesis,
        "action": action,
        "related_memory_hypothesis": related_memory_hypothesis,
        "observations": [],
        "conclusion": None,
        "status": "unknown",
        "closed": False,
    }
    write_json(os.path.join(path, "experiment.json"), metadata)

    if hypothesis is not None:
        with open(os.path.join(path, "hypothesis.md"), "w", encoding="utf-8") as file:
            file.write(hypothesis.rstrip() + "\n")
    if activate:
        activate_experiment(target, experiment_id)

    return {
        "experiment_id": experiment_id,
        "path": relative_path(target, path),
    }


def append_observation(
    target: str,
    experiment_id: str,
    observation: str,
    artifact: str = None,
) -> dict:
    metadata = load_experiment(target, experiment_id)
    if metadata.get("closed"):
        raise ValueError("experiment 已关闭")

    entry = {
        "recorded_at": now_iso(),
        "observation": observation,
        "artifact": artifact,
    }
    metadata["observations"].append(entry)
    write_json(metadata_path(target, experiment_id), metadata)

    output_path = os.path.join(experiment_dir(target, experiment_id), "output.txt")
    with open(output_path, "a", encoding="utf-8") as file:
        file.write(f"[{entry['recorded_at']}]\n{observation.rstrip()}\n")
        if artifact:
            file.write(f"artifact: {artifact}\n")
        file.write("\n")

    return {
        "experiment_id": experiment_id,
        "observation_count": len(metadata["observations"]),
    }


def _load_merge_module():
    """按需加载 merge 模块（与 assemble 相同的导入回退模式，无循环依赖）。"""
    try:
        from runtime import merge  # noqa: F401
        return merge
    except ImportError:
        runtime_dir = os.path.join(PROJECT_ROOT, "runtime")
        if runtime_dir not in sys.path:
            sys.path.insert(0, runtime_dir)
        import merge  # noqa: F401
        return merge


def _parse_memory_update(raw: str):
    """解析 --memory-update JSON；非法输入返回 None（不阻断 close）。"""
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except (json.JSONDecodeError, TypeError):
        pass
    print(f"[experiment] WARN --memory-update 非合法 JSON: {str(raw)[:80]}", file=sys.stderr)
    return None


def _sync_memory(
    target: str,
    experiment_id: str,
    status: str,
    memory_update: dict,
    artifacts: list,
) -> dict | None:
    """close 后把 LLM 显式声明的探索结果机械写回 memory。

    Runtime 只做三件事：
      1. target 校验（不一致 → 写 results/<safe>/memory.md 归档，不污染 active）
      2. 查重 / 替换（同 text 行已存在 → 更新状态；否则 ADD）
      3. 写入 memory
    禁止：从 conclusion 抽取语义、判断漏洞类型、自动生成关系。

    memory_update 结构（由 LLM 提供，见 --memory-update）：
      {"text": "<探索后的世界变化>", "status": "confirmed", "evidence": ["req_747"]}
    text 是探索后的结论（不是 create 时的探索起点假设）。
    status 缺省用实验最终状态映射；evidence 缺省用实验观察里登记的 artifact。
    """
    if not memory_update:
        return None
    text = (memory_update.get("text") or "").strip()
    if not text:
        print("[experiment] WARN --memory-update 缺少 text，跳过 memory 同步", file=sys.stderr)
        return None

    state = memory_update.get("status") or MEMORY_STATUS_MAP.get(status, "observed")
    if state not in VALID_MEMORY_STATES:
        state = "observed"

    evidence = memory_update.get("evidence") or artifacts or []
    if isinstance(evidence, str):
        evidence = [evidence]
    evidence = [str(e).strip() for e in evidence if str(e).strip()]

    merge = _load_merge_module()
    lines = merge.read_memory()

    active_body = merge.extract_active_target(lines) if hasattr(merge, "extract_active_target") else ""
    active_id = merge.target_identity(active_body) if active_body else ""
    target_id = merge.target_identity(target)

    # 1) target 校验：与 active 不一致 → 落到该 target 归档文件
    dest = None
    if active_id and target_id and active_id != target_id:
        safe = merge.target_safe_name(target)
        dest = os.path.join(RESULTS_DIR, safe, "memory.md")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            with open(dest, "r", encoding="utf-8") as f:
                lines = f.readlines()
        else:
            lines = []

    # 2) 查重 / 替换：同 text 的 hypothesis 行已存在 → 替换状态；否则 ADD
    hypothesis_line = f"hypothesis [{state}]: {text}"
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("hypothesis [") and "]:" in stripped:
            content = stripped.split("]:", 1)[1].strip()
            if content == text:
                lines[i] = hypothesis_line + "\n"
                replaced = True
                break
    if not replaced:
        lines = merge.add_line(lines, hypothesis_line)

    # evidence 引用：已有含本实验编号的 artifact 行 → 替换；否则 ADD
    if evidence:
        artifact_line = "artifact: " + experiment_id + "; " + ", ".join(evidence)
        replaced_art = False
        for i, line in enumerate(lines):
            if line.strip().startswith("artifact:") and experiment_id in line:
                lines[i] = artifact_line + "\n"
                replaced_art = True
                break
        if not replaced_art:
            lines = merge.add_line(lines, artifact_line)

    # 3) 写入
    if dest is not None:
        with open(dest, "w", encoding="utf-8") as f:
            f.write("".join(lines))
    else:
        merge.write_memory(lines)

    return {
        "memory_synced": True,
        "line": hypothesis_line,
        "evidence_count": len(evidence),
        "written_to": "active" if dest is None else "archive",
    }


def close_experiment(
    target: str,
    experiment_id: str = None,
    conclusion: str = None,
    status: str = "unknown",
    memory_update: dict = None,
) -> dict:
    if status not in FINAL_STATUSES:
        raise ValueError(f"status 必须是 {', '.join(sorted(FINAL_STATUSES))} 之一")

    experiment_id = resolve_experiment_id(target, experiment_id)
    if experiment_id is None:
        raise ValueError("没有可关闭的 active experiment")
    metadata = load_experiment(target, experiment_id)
    metadata.update({
        "closed_at": now_iso(),
        "conclusion": conclusion,
        "status": status,
        "closed": True,
    })
    write_json(metadata_path(target, experiment_id), metadata)
    deactivate_experiment(target, experiment_id)

    artifacts = list(dict.fromkeys(
        entry["artifact"] for entry in metadata["observations"] if entry.get("artifact")
    ))
    evidence_path = os.path.join(experiment_dir(target, experiment_id), "evidence.md")
    with open(evidence_path, "w", encoding="utf-8") as file:
        if conclusion is not None:
            file.write(conclusion.rstrip() + "\n\n")
        file.write(f"status: {status}\n")
        if artifacts:
            file.write("\nartifacts:\n")
            for artifact in artifacts:
                file.write(f"- {artifact}\n")

    memory_sync = _sync_memory(target, experiment_id, status, memory_update, artifacts)

    return {
        "experiment_id": experiment_id,
        "status": status,
        "closed": True,
        "evidence": relative_path(target, evidence_path),
        "artifact_count": len(artifacts),
        "memory_sync": memory_sync,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="目标级实验事实管理")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("target")
    create.add_argument("--experiment-id")
    create.add_argument("--hypothesis")
    create.add_argument("--action")
    create.add_argument("--activate", action="store_true")
    create.add_argument("--related-memory-hypothesis")

    activate = subparsers.add_parser("activate")
    activate.add_argument("target")
    activate.add_argument("experiment_id")

    deactivate = subparsers.add_parser("deactivate")
    deactivate.add_argument("target")
    deactivate.add_argument("experiment_id", nargs="?")

    append = subparsers.add_parser("append")
    append.add_argument("target")
    append.add_argument("experiment_id")
    append.add_argument("--observation", required=True)
    append.add_argument("--artifact")

    close = subparsers.add_parser("close")
    close.add_argument("target")
    close.add_argument("experiment_id", nargs="?")
    close.add_argument("--conclusion")
    close.add_argument("--status", choices=sorted(FINAL_STATUSES), default="unknown")
    close.add_argument(
        "--memory-update",
        type=_parse_memory_update,
        metavar='{"text": "...", "status": "confirmed", "evidence": ["req_747"]}',
        help="close 后机械写回 memory 的探索结果（JSON）。text=结论；status=confirmed/"
        "contradicted/blocked/observed（缺省由 --status 映射）；evidence=证据编号数组",
    )

    args = parser.parse_args()
    if args.command == "create":
        result = create_experiment(
            args.target, 
            args.hypothesis, 
            args.action, 
            args.experiment_id, 
            args.activate,
            getattr(args, 'related_memory_hypothesis', None)
        )
    elif args.command == "activate":
        result = activate_experiment(args.target, args.experiment_id)
    elif args.command == "deactivate":
        result = deactivate_experiment(args.target, args.experiment_id)
    elif args.command == "append":
        result = append_observation(args.target, args.experiment_id, args.observation, args.artifact)
    else:
        result = close_experiment(
            args.target,
            args.experiment_id,
            args.conclusion,
            args.status,
            getattr(args, "memory_update", None),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (FileExistsError, FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        sys.exit(1)
