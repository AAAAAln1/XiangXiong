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


def close_experiment(
    target: str,
    experiment_id: str = None,
    conclusion: str = None,
    status: str = "unknown",
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

    return {
        "experiment_id": experiment_id,
        "status": status,
        "closed": True,
        "evidence": relative_path(target, evidence_path),
        "artifact_count": len(artifacts),
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
        result = close_experiment(args.target, args.experiment_id, args.conclusion, args.status)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (FileExistsError, FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        sys.exit(1)
