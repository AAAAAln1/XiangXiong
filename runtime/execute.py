#!/usr/bin/env python3
"""
execute.py — 实验命令受控执行器。

职责：
- 执行命令
- 施加超时
- 拒绝未声明的交互式命令
- 返回结构化执行结果
- 写入内部 metadata

不职责：
- 不理解安全业务
- 不判断实验意义
- 不写 memory.md
- 不选择下一步
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

try:
    from .experiment import append_observation, resolve_experiment_id
except ImportError:
    from experiment import append_observation, resolve_experiment_id

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")
DEFAULT_TIMEOUT = 60
MAX_OUTPUT_CHARS = 4096
INTERACTIVE_COMMANDS = {"telnet", "ssh", "ftp", "sftp", "python", "python3", "cmd", "powershell", "pwsh", "bash", "sh"}
SHELL_BUILTINS = {"echo", "dir", "cd", "copy", "type", "set", "where"}


class ExperimentAssociationError(RuntimeError):
    def __init__(self, message: str, result: dict):
        super().__init__(message)
        self.result = result


def safe_target(target: str) -> str:
    return target.replace(":", "_").replace("/", "_").replace("\\", "_")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def first_command_token(command: str) -> str:
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        parts = command.split()
    if not parts:
        return ""
    token = parts[0].strip('"\'')
    return os.path.basename(token).lower().removesuffix(".exe")


def interactive_warning(command: str) -> dict:
    token = first_command_token(command)
    if token not in INTERACTIVE_COMMANDS:
        return None

    parts = command.split()
    detected = False
    if token in {"telnet", "ftp", "sftp"}:
        detected = True
    elif token == "ssh" and len(parts) <= 2:
        detected = True
    elif token in {"python", "python3", "cmd", "powershell", "pwsh", "bash", "sh"} and len(parts) == 1:
        detected = True

    if not detected:
        return None
    return {
        "status": "warning",
        "reason": "interactive_command_detected",
        "message": "command may require interaction",
    }


def ensure_metadata_dir(target: str) -> str:
    path = os.path.join(RESULTS_DIR, safe_target(target), "experiments", "_runtime")
    os.makedirs(path, exist_ok=True)
    return path


def relative_result_path(target: str, path: str) -> str:
    target_root = os.path.join(RESULTS_DIR, safe_target(target))
    return os.path.relpath(path, target_root).replace("\\", "/")


def persist_execution(target: str, metadata: dict, stdout: str, stderr: str) -> str:
    save_dir = ensure_metadata_dir(target)
    execution_id = metadata["execution_id"]
    stdout_path = os.path.join(save_dir, f"{execution_id}.stdout.txt")
    stderr_path = os.path.join(save_dir, f"{execution_id}.stderr.txt")
    metadata_path = os.path.join(save_dir, f"{execution_id}.json")

    with open(stdout_path, "w", encoding="utf-8", errors="replace") as file:
        file.write(stdout)
    with open(stderr_path, "w", encoding="utf-8", errors="replace") as file:
        file.write(stderr)

    metadata["artifacts"] = {
        "stdout": relative_result_path(target, stdout_path),
        "stderr": relative_result_path(target, stderr_path),
    }
    with open(metadata_path, "w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)
    return relative_result_path(target, metadata_path)


def trim_output(value: str) -> str:
    if len(value) <= MAX_OUTPUT_CHARS:
        return value
    return value[:MAX_OUTPUT_CHARS] + f"\n...[truncated to {MAX_OUTPUT_CHARS} chars]"


def command_exists(command: str) -> bool:
    token = first_command_token(command)
    if not token:
        return False
    if token in SHELL_BUILTINS:
        return True
    if os.path.dirname(token):
        return os.path.exists(token)
    return shutil.which(token) is not None


def terminate_process_tree(proc: subprocess.Popen):
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_command(command: str, timeout: int) -> dict:
    started = time.time()
    if not command_exists(command):
        return {
            "status": "tool_not_found",
            "exit_code": None,
            "stdout": "",
            "stderr": f"tool not found: {first_command_token(command)}",
            "duration": 0,
            "reason": "tool not found",
        }
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_process_tree(proc)
            stdout, stderr = proc.communicate(timeout=5)
            return {
                "status": "timeout",
                "exit_code": None,
                "stdout": stdout or "",
                "stderr": stderr or "",
                "duration": round(time.time() - started, 3),
                "reason": f"execution exceeded timeout {timeout}s",
            }

        status = "success" if proc.returncode == 0 else "failed"
        return {
            "status": status,
            "exit_code": proc.returncode,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "duration": round(time.time() - started, 3),
            "reason": None,
        }
    except FileNotFoundError as exc:
        return {
            "status": "tool_not_found",
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "duration": round(time.time() - started, 3),
            "reason": "tool not found",
        }


def build_result(args) -> dict:
    execution_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    experiment_id = resolve_experiment_id(args.target, args.experiment_id)
    warning = interactive_warning(args.command)
    metadata = {
        "execution_id": execution_id,
        "experiment_id": experiment_id,
        "created_time": now_iso(),
        "target": args.target,
        "experiment": args.name,
        "purpose": args.purpose,
        "command": args.command,
        "timeout": args.timeout,
        "runtime": "subprocess.run(shell=True)",
        "status": "running",
        "warning": warning,
    }

    execution = run_command(args.command, args.timeout)
    metadata.update({
        "finished_time": now_iso(),
        "status": execution["status"],
        "duration": execution["duration"],
        "exit_code": execution["exit_code"],
        "reason": execution["reason"],
    })
    metadata_path = persist_execution(
        args.target, metadata, execution["stdout"], execution["stderr"]
    )

    result = {
        "execution_id": execution_id,
        "experiment_id": experiment_id,
        "experiment": args.name,
        "purpose": args.purpose,
        "command": args.command,
        "warning": warning,
        "status": execution["status"],
        "duration": f"{execution['duration']}s",
        "timeout": f"{args.timeout}s",
        "exit_code": execution["exit_code"],
        "reason": execution["reason"],
        "metadata": metadata_path,
        "stdout": trim_output(execution["stdout"]),
        "stderr": trim_output(execution["stderr"]),
    }

    if experiment_id:
        observation = (
            f"command: {args.command}\n"
            f"status: {execution['status']}\n"
            f"exit_code: {execution['exit_code']}\n"
            f"duration: {execution['duration']}s\n"
            f"reason: {execution['reason']}\nmetadata_artifact: {metadata_path}"
        )
        try:
            append_observation(args.target, experiment_id, observation, metadata_path)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
            raise ExperimentAssociationError(
                f"无法关联实验 {experiment_id}: {error}; 执行记录已保存至 {metadata_path}",
                result,
            ) from error

    return result


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="受控执行实验命令")
    parser.add_argument("target", help="结果归档目标名")
    parser.add_argument("--name", default="experiment", help="实验名称")
    parser.add_argument("--experiment-id", help="外部传入的实验 ID")
    parser.add_argument("--purpose", help="实验目的，仅保存不解释")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="超时时间，默认 60 秒")
    parser.add_argument("--cmd", required=True, help="要执行的命令字符串")
    args = parser.parse_args()
    args.command = args.cmd.strip()
    if not args.command:
        parser.error("empty command")

    try:
        result = build_result(args)
    except ExperimentAssociationError as error:
        failed_result = dict(error.result)
        failed_result["association_error"] = str(error)
        print(json.dumps(failed_result, ensure_ascii=False, indent=2))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
