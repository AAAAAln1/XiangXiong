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
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

try:
    from .experiment import append_observation, resolve_experiment_id
    from .paths import safe_name as safe_target
    from . import scope as _scope
    from . import turn_state
except ImportError:
    from experiment import append_observation, resolve_experiment_id
    from paths import safe_name as safe_target
    import scope as _scope
    import turn_state

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 允许环境覆盖（detached worker 是独立进程，不继承调用方的 monkeypatch，但继承环境变量）
RESULTS_DIR = os.environ.get("XIANGXIONG_RESULTS_DIR") or os.path.join(PROJECT_ROOT, "results")
DEFAULT_TIMEOUT = 60
MAX_OUTPUT_CHARS = 4096
INTERACTIVE_COMMANDS = {"telnet", "ssh", "ftp", "sftp", "python", "python3", "cmd", "powershell", "pwsh", "bash", "sh"}
SHELL_BUILTINS = {"echo", "dir", "cd", "copy", "type", "set", "where"}


class ExperimentAssociationError(RuntimeError):
    def __init__(self, message: str, result: dict):
        super().__init__(message)
        self.result = result


class ScopeRejected(Exception):
    """第二层授权闸拒绝（未执行命令）。message 是可抄回执。"""


# 命令行里被引用的脚本文件：results/<t>/scripts/ 下的路径形态
_SCRIPT_PATH_RE = re.compile(
    r"results[\\/][^\s\"'|&<>]+?[\\/]scripts[\\/][^\s\"'|&<>]+\.(?:py|ps1|bat|cmd|sh|rb|pl)",
    re.IGNORECASE,
)


def enforce_scope_command(target: str, command: str) -> None:
    """第二层（best-effort）静态授权闸：扫描命令串与引用脚本中的私网 IP/CIDR 字面量。

    诚实边界：拦不住运行时动态目标（脚本运行时读出的 IP、DNS 反弹、工具内部外联）。
    http_request 发包前另有第一层结构化硬闸；本层只覆盖 execute 的自由 shell。
    """
    # 无 scope 文件时：公网/无目标命令放行；含私网字面量 → 拒（要求先 init）
    texts = [command or ""]
    for m in _SCRIPT_PATH_RE.finditer(command or ""):
        rel = m.group(0).replace("\\", os.sep).replace("/", os.sep)
        candidates = [os.path.join(PROJECT_ROOT, rel), rel]
        for cand in candidates:
            if os.path.isfile(cand):
                try:
                    with open(cand, "r", encoding="utf-8", errors="replace") as f:
                        texts.append(f.read())
                except OSError:
                    pass
                break

    for text in texts:
        for token in _scope.extract_ip_literals(text):
            ip = token.split("/", 1)[0]
            private = _scope.is_private_literal(token)
            if not private:
                # 公网字面量：engagement 默认禁出公网（egress 空）；非 engagement 放行
                if _scope.has_scope(target):
                    decision = _scope.evaluate(target, ip)
                    if decision["decision"] not in ("allow", "local"):
                        raise ScopeRejected(_scope.reject_message(target, token, decision))
                continue
            # 私网字面量
            if not _scope.has_scope(target):
                decision = {"decision": "no_scope", "matched": ""}
                raise ScopeRejected(_scope.reject_message(target, token, decision))
            decision = _scope.evaluate(target, ip)
            if decision["decision"] in ("deny", "out_of_scope", "no_scope"):
                raise ScopeRejected(_scope.reject_message(target, token, decision))


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


# ── 后台任务（P1-a）：父进程分配 id/写 job/detached spawn；worker 负责执行+落盘+登记 art ──
JOBS_SUBDIR = "jobs"
JOB_LOCK_NAME = ".jobs_lock"
JOB_ID_RE_TEMPLATE = "exec-{:03d}.json"
_TERMINAL_JOB_STATES = ("completed", "failed", "timeout", "scope_rejected", "cancelled", "orphaned")


def jobs_dir(target: str) -> str:
    path = os.path.join(RESULTS_DIR, safe_target(target), JOBS_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def job_path(target: str, job_id: str) -> str:
    return os.path.join(jobs_dir(target), job_id + ".json")


def _next_job_id(target: str) -> str:
    """filelock 内扫 exec-NNN 分配下一个后台 id（与 experiment/art 编号同手法）。"""
    d = jobs_dir(target)
    highest = 0
    for name in os.listdir(d):
        m = re.match(r"^exec-(\d{3,})\.json$", name)
        if m:
            highest = max(highest, int(m.group(1)))
    return f"exec-{highest + 1:03d}"


def _atomic_write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_job(target: str, job_id: str) -> dict:
    with open(job_path(target, job_id), "r", encoding="utf-8") as f:
        return json.load(f)


def _write_job(target: str, job_id: str, data: dict) -> None:
    _atomic_write_json(job_path(target, job_id), data)


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        kernel32.CloseHandle(h)
        STILL_ACTIVE = 259
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def start_background(args) -> dict:
    """父进程：预检 scope → 分配 id → 写 job → detached spawn worker，立即返回。"""
    # 闸在 spawn 前（worker 启动前在父进程做，REJECT 则不 spawn、不留 job）
    enforce_scope_command(args.target, args.command)
    if not command_exists(args.command):
        raise ScopeRejected(f"[SCOPE/EXEC] tool_not_found: {first_command_token(args.command)}")

    d = jobs_dir(args.target)
    try:
        from filelock import locked
    except ImportError:
        from .filelock import locked
    with locked(os.path.join(d, JOB_LOCK_NAME)):
        job_id = _next_job_id(args.target)
        jp = job_path(args.target, job_id)
        job = {
            "id": job_id,
            "target": args.target,
            "cmd": args.command,
            "timeout": args.timeout,
            "name": args.name,
            "purpose": args.purpose,
            "experiment_id": args.experiment_id,
            "max_output": args.max_output,
            "status": "queued",
            "pid": None,
            "created": now_iso(),
            "artifacts": None,
            "metadata": None,
        }
        _atomic_write_json(jp, job)

        worker_cmd = [sys.executable, os.path.abspath(__file__), "--worker", "--job", jp]
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                worker_cmd,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                worker_cmd,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
    return job_id


def run_worker(job_file: str) -> int:
    """worker 进程：跑命令 → 落盘/登记 art（在本进程内完成，不丢）→ 原子改 job。"""
    import types

    with open(job_file, "r", encoding="utf-8") as f:
        job = json.load(f)
    target = job["target"]
    job["status"] = "running"
    job["pid"] = os.getpid()
    _atomic_write_json(job_file, job)

    args = types.SimpleNamespace(
        target=target,
        command=job["cmd"],
        timeout=job.get("timeout", DEFAULT_TIMEOUT),
        name=job.get("name", "experiment"),
        purpose=job.get("purpose"),
        experiment_id=job.get("experiment_id"),
        max_output=job.get("max_output") or MAX_OUTPUT_CHARS,
    )
    terminal = "completed"
    try:
        result = build_result(args)
        if result.get("status") in ("failed", "timeout", "tool_not_found"):
            terminal = result["status"]
        job["status"] = terminal
        job["artifacts"] = result.get("artifact_ids")
        job["metadata"] = result.get("metadata")
        job["exit_code"] = result.get("exit_code")
    except ScopeRejected as exc:
        job["status"] = "scope_rejected"
        job["error"] = str(exc)
    except Exception as exc:  # worker 崩溃也必须落状态，不残留 running
        job["status"] = "failed"
        job["error"] = repr(exc)
    job["finished"] = now_iso()
    _atomic_write_json(job_file, job)
    return 0


def job_status(target: str, job_id: str) -> dict:
    job = _read_job(target, job_id)
    if job["status"] in ("queued", "running") and not _pid_alive(job.get("pid")):
        if job.get("pid"):
            job["status"] = "orphaned"
            _write_job(target, job_id, job)
    return job


def wait_job(target: str, job_id: str, timeout_s: float) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = job_status(target, job_id)
        if job["status"] in _TERMINAL_JOB_STATES:
            return job
        time.sleep(0.5)
    return job_status(target, job_id)


def cancel_job(target: str, job_id: str) -> dict:
    job = _read_job(target, job_id)
    if job["status"] in _TERMINAL_JOB_STATES:
        return job
    pid = job.get("pid")
    if pid:
        # 树级终止（与同步超时同一手法，Windows 杀孙进程）
        try:
            terminate_process_tree(type("P", (), {"pid": pid})())
        except Exception:
            pass
    job["status"] = "cancelled"
    job["finished"] = now_iso()
    _write_job(target, job_id, job)
    return job


def relative_result_path(target: str, path: str) -> str:
    target_root = os.path.join(RESULTS_DIR, safe_target(target))
    return os.path.relpath(path, target_root).replace("\\", "/")


def _artifacts_index_path(target: str) -> str:
    return os.path.join(RESULTS_DIR, safe_target(target), "artifacts", "index.json")


def _next_artifact_id(index: dict) -> str:
    highest = 0
    for name in index:
        match = re.match(r"^art-(\d+)$", name)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"art-{highest + 1:03d}"


def register_artifact(target: str, path: str, *, source_cmd=None, kind=None) -> str:
    """把落盘文件登记为可解析证据锚点 art-NNN，返回该编号。

    根因（本函数补的就是它）：Evidence Gate（merge.py）只认 req_/exp-/art-，
    而全仓此前**只有读方没有写方** —— artifacts/index.json 无任何生产者，
    art- 通道恒空。后果：非 HTTP 目标（CTF/逆向/取证，走 execute.py）没有 req_NNN，
    若引 results/ 文件路径写 counter:/vuln: 必被 REJECT。
    与 experiment.next_experiment_id 同构：扫最大值 +1，机械分配，不判断内容。

    并发（2026-10-06 P0-d）：read-modify-write 包进 .artifacts.lock。CLAUDE.md 鼓励
    一条消息并行多个 execute（background worker 上线后并发成常态），无锁即 lost update。
    """
    index_path = _artifacts_index_path(target)
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    try:
        from filelock import locked
    except ImportError:
        from .filelock import locked

    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            digest.update(chunk)

    with locked(os.path.join(os.path.dirname(index_path), ".artifacts.lock")):
        try:
            with open(index_path, "r", encoding="utf-8") as file:
                index = json.load(file)
            if not isinstance(index, dict):
                index = {}
        except (OSError, json.JSONDecodeError):
            index = {}

        artifact_id = _next_artifact_id(index)
        index[artifact_id] = {
            "sha256": digest.hexdigest(),
            "size": os.path.getsize(path),
            "kind": kind,
            "source_cmd": source_cmd,
            "created": now_iso(),
            "path": os.path.basename(path),
        }
        tmp = index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(index, file, ensure_ascii=False, indent=2)
        os.replace(tmp, index_path)
    return artifact_id


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
    # 登记为 art-NNN 证据锚点（空文件不登记，避免 index 被噪音占满）。
    # 这样 Agent 可以直接用 art-NNN 写 counter:/vuln:，而不是引 results/ 路径被闸门拒掉。
    artifact_ids = {}
    if stdout:
        artifact_ids["stdout"] = register_artifact(
            target, stdout_path, source_cmd=metadata.get("command"), kind="stdout"
        )
    if stderr:
        artifact_ids["stderr"] = register_artifact(
            target, stderr_path, source_cmd=metadata.get("command"), kind="stderr"
        )
    metadata["artifact_ids"] = artifact_ids
    with open(metadata_path, "w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)
    # F0：登记最近 art 编号（stdout 优先，空则 stderr），供 merge REJECT 回执提示。全静默。
    latest_art = artifact_ids.get("stdout") or artifact_ids.get("stderr")
    if latest_art:
        turn_state.record(target, "art", latest_art)
    return relative_result_path(target, metadata_path)


def trim_output(value: str, max_chars: int = MAX_OUTPUT_CHARS, artifact_path: str = None) -> str:
    """回显裁剪：超长时**头尾各半**（渗透结论常在尾部）+ 全文指针。

    2026-10-06 P1-a：原实现 prefix-only，nmap/impacket 长输出尾部结论（开放端口、
    命令回显、flag）被丢，且无法判断扫描完整性。全文始终已落盘并登记 art-NNN，
    这里只改回显形态，路径必须完整可读（学 DRT context_budget：路径比预览更重要）。
    """
    if len(value) <= max_chars:
        return value
    head = value[: max_chars // 2]
    tail = value[len(value) - max_chars // 2 :]
    marker = f"\n...[stdout/stderr {len(value)} 字符；头 {max_chars // 2} + 尾 {max_chars // 2}；全文"
    if artifact_path:
        marker += f": {artifact_path}"
    marker += "]..."
    return head + marker + tail


def command_exists(command: str) -> bool:
    """检查命令首 token 是否存在。绝对路径（含目录）直接 exists；
    无目录的命令名走 which（SHELL_BUILTINS 放行）。不用 first_command_token——
    它会把绝对路径剥成纯文件名，导致绝对路径误判 tool_not_found。"""
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        parts = command.split()
    if not parts:
        return False
    raw_token = parts[0].strip('"\'')
    if raw_token in SHELL_BUILTINS:
        return True
    if os.path.dirname(raw_token):
        return os.path.exists(raw_token)
    token = os.path.basename(raw_token).lower().removesuffix(".exe")
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

    # 第二层授权闸（best-effort 字面量静态检查），越界在执行前抛出
    enforce_scope_command(args.target, args.command)
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

    max_output = getattr(args, "max_output", None) or MAX_OUTPUT_CHARS
    stdout_path = metadata.get("artifacts", {}).get("stdout") if metadata.get("artifacts") else None
    stderr_path = metadata.get("artifacts", {}).get("stderr") if metadata.get("artifacts") else None
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
        "saved": metadata.get("artifacts"),
        "artifact_ids": metadata.get("artifact_ids"),
        "stdout": trim_output(execution["stdout"], max_output, stdout_path),
        "stderr": trim_output(execution["stderr"], max_output, stderr_path),
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

    # worker 内部模式（detached 子进程，由 start_background spawn）
    if "--worker" in sys.argv:
        wparser = argparse.ArgumentParser()
        wparser.add_argument("--worker", action="store_true")
        wparser.add_argument("--job", required=True)
        wargs = wparser.parse_args()
        sys.exit(run_worker(wargs.job))

    parser = argparse.ArgumentParser(description="受控执行实验命令")
    parser.add_argument("target", help="结果归档目标名")
    parser.add_argument("--name", default="experiment", help="实验名称")
    parser.add_argument("--experiment-id", help="外部传入的实验 ID")
    parser.add_argument("--purpose", help="实验目的，仅保存不解释")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="超时时间，默认 60 秒")
    parser.add_argument("--cmd", help="要执行的命令字符串")
    parser.add_argument("--max-output", type=int, default=MAX_OUTPUT_CHARS,
                        help=f"回显字符上限（默认 {MAX_OUTPUT_CHARS}）；全文始终落盘，超限时头尾各半")
    # 后台任务
    parser.add_argument("--background", action="store_true", help="后台执行，立即返回 exec-NNN")
    parser.add_argument("--bg-status", metavar="EXEC_ID", help="查后台任务状态")
    parser.add_argument("--bg-wait", metavar="EXEC_ID", help="等待后台任务结束")
    parser.add_argument("--bg-cancel", metavar="EXEC_ID", help="取消后台任务（树级终止）")
    parser.add_argument("--wait-timeout", type=int, default=30, help="--bg-wait 等待秒数")
    args = parser.parse_args()

    # 后台控制子命令（不需要 --cmd）
    if args.bg_status:
        print(json.dumps(job_status(args.target, args.bg_status), ensure_ascii=False, indent=2))
        return
    if args.bg_wait:
        job = wait_job(args.target, args.bg_wait, args.wait_timeout)
        print(json.dumps(job, ensure_ascii=False, indent=2))
        sys.exit(0 if job["status"] in ("completed",) else 1)
    if args.bg_cancel:
        print(json.dumps(cancel_job(args.target, args.bg_cancel), ensure_ascii=False, indent=2))
        return

    if not args.cmd:
        parser.error("--cmd 必填（后台控制用 --bg-status/--bg-wait/--bg-cancel）")
    args.command = args.cmd.strip()
    if not args.command:
        parser.error("empty command")

    if args.background:
        try:
            job_id = start_background(args)
        except ScopeRejected as error:
            print(str(error), file=sys.stderr)
            sys.exit(2)
        print(f"[BACKGROUND] {job_id} queued（{args.command[:80]}）")
        print(f"  状态: python runtime/execute.py {args.target} --bg-status {job_id}")
        print(f"  等待: python runtime/execute.py {args.target} --bg-wait {job_id} [--wait-timeout 30]")
        print(f"  取消: python runtime/execute.py {args.target} --bg-cancel {job_id}")
        return

    try:
        result = build_result(args)
    except ScopeRejected as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
    except ExperimentAssociationError as error:
        failed_result = dict(error.result)
        failed_result["association_error"] = str(error)
        print(json.dumps(failed_result, ensure_ascii=False, indent=2))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
