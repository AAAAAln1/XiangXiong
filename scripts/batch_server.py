#!/usr/bin/env python3
"""
batch_server.py — 批量渗透调度器 + 轻量 HTTP 仪表盘（单进程，零依赖）。

职责（纯派发 + 状态 + 报告校验，零判断）：
- 接收 target 列表（UI 输入，须显式授权确认），排队调度
- 并发 spawn N 个 `claude -p` worker（完整 xiangxiong：保留 hooks，env 绑定独立 world）
- 每 worker stdout(stream-json) 逐行落 logs/<host>.jsonl，供前端轮询实时投影
- 进程退出后机械校验 report.md（存在/非空/含漏洞特征），状态标注
- state.json 仅本进程写（内存锁保护），worker 完全不碰

理念：
- 每 target 单 worker（调度器保证）；不同 target 独立 world，物理隔离
- target 标识符统一用 host（identity 与 http_request/merge 的 results/ 目录一致）
- 调度器不判断漏洞、不写 world、不注入认知；授权责任在调用方（UI 授权确认）
- 完成判定 = claude -p 自然退出（--max-turns 兜底）+ 报告机械校验；无提前收口机制

用法：
  python scripts/batch_server.py [--port 8787] [--concurrency 2] [--max-turns 100]
  python scripts/batch_server.py --add https://example.com   # CLI 添加目标（非交互）
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATCH_DIR = os.path.join(PROJECT_ROOT, "batch")
LOGS_DIR = os.path.join(BATCH_DIR, "logs")
RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")
STATE_PATH = os.path.join(BATCH_DIR, "state.json")
STATIC_DIR = os.path.join(BATCH_DIR, "static")

DEFAULT_CONCURRENCY = 2
DEFAULT_MAX_TURNS = 100
DEFAULT_TIMEOUT_S = 7200  # 进程级 hang 保护（2h，远大于正常轮数耗时）
DEFAULT_EVENTS_TAIL = 200
DEFAULT_CLAUDE_BIN = "claude"

# ── 全局状态（仅调度进程写）──
_STATE_LOCK = threading.Lock()
_QUEUE: list = []                 # [host, ...] pending
_ACTIVE: dict = {}                # host -> Popen
_STOP = threading.Event()


def _safe_host(host: str) -> str:
    return host.replace(":", "_").replace("/", "_").replace("\\", "_")


def host_of(input_str: str) -> str:
    """从 URL / host 输入提取纯 host（小写，去协议/path/端口语义仅留主机段）。"""
    s = (input_str or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/")[0]
    return s.strip()


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"version": 1, "targets": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "targets": {}}


def save_state(state: dict) -> None:
    os.makedirs(BATCH_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _update_target(host: str, **extra) -> None:
    with _STATE_LOCK:
        state = load_state()
        entry = state["targets"].setdefault(host, {"host": host})
        entry.update(extra)
        entry["updated_at"] = time.strftime("%H:%M:%S")
        save_state(state)


# ── worker ──
WORKER_PROMPT = """授权目标：{url}。你是象雄黑盒渗透测试智能体（xiangxiong）。

本会话 target 标识符已锁定为 {host}（world 已初始化），DELTA 禁止写 target: 行。
URL 用 {url} 作为请求基础。完整遵守项目根 CLAUDE.md 的契约（工具用法/记账/探索收口/report 格式）。
SessionStart 投影会给出当前 world 状态；从授权目标开始渗透测试。
最终必须产出 results/{safe_host}/report.md（格式见 CLAUDE.md report 节，一洞一节带原始请求包）。
完成时输出 [DONE] 并结束。"""


def init_world(host: str) -> str:
    """预初始化 world 文件（target: host 行），避免 agent 误写 target 触发归档。"""
    world_path = os.path.join(RESULTS_DIR, _safe_host(host), "memory.md")
    os.makedirs(os.path.dirname(world_path), exist_ok=True)
    if not os.path.exists(world_path):
        with open(world_path, "w", encoding="utf-8") as f:
            f.write(f"target: {host}\n")
    return world_path


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except Exception:
            pass
    else:
        proc.kill()


def run_worker(host: str, base_url: str, max_turns: int, timeout_s: int,
               claude_bin: str = DEFAULT_CLAUDE_BIN) -> None:
    world_path = init_world(host)
    log_path = os.path.join(LOGS_DIR, f"{_safe_host(host)}.jsonl")
    os.makedirs(LOGS_DIR, exist_ok=True)

    prompt = WORKER_PROMPT.format(
        url=base_url, host=host, safe_host=_safe_host(host),
    )
    env = dict(os.environ)
    env["XIANGXIONG_WORLD_ACTIVE"] = world_path

    cmd = [
        claude_bin, "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--max-turns", str(max_turns),
        prompt,
    ]
    started = time.time()
    _update_target(host, status="running", started_at=time.strftime("%H:%M:%S"))

    try:
        proc = subprocess.Popen(
            cmd, cwd=PROJECT_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        _update_target(host, status="failed", error="claude CLI not found on PATH",
                       duration_s=0)
        return
    _ACTIVE[host] = proc

    logf = open(log_path, "w", encoding="utf-8", errors="replace")

    def _read_stdout() -> None:
        try:
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
        except Exception:
            pass

    reader = threading.Thread(target=_read_stdout, daemon=True)
    reader.start()

    # 主循环：poll 检测退出 + 超时 hang 保护
    deadline = time.time() + timeout_s
    status = None
    while True:
        code = proc.poll()
        if code is not None:
            status = "done" if code == 0 else f"failed"
            _update_target(host, exit_code=code)
            break
        if time.time() > deadline:
            _kill_tree(proc)
            status = "timeout"
            _update_target(host, error=f"hard timeout {timeout_s}s")
            break
        time.sleep(1)

    _ACTIVE.pop(host, None)
    proc.wait(timeout=10)
    reader.join(timeout=5)
    logf.close()

    duration_s = round(time.time() - started, 1)
    _update_target(host, status=status, duration_s=duration_s)

    # 报告机械校验
    if status == "done":
        _update_target(host, report_validation=validate_report(host))


def validate_report(host: str) -> str:
    """机械校验 report.md：存在/非空/含漏洞特征。不判断真假。"""
    path = os.path.join(RESULTS_DIR, _safe_host(host), "report.md")
    if not os.path.exists(path):
        return "missing"
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return "unreadable"
    if len(text.strip()) < 100:
        return "too_short"
    if not re.search(
        r"(vuln|漏洞|复现|HTTP/1\.\d|CVE-|注入|越权|未授权|XSS|SQL)", text,
        re.IGNORECASE,
    ):
        return "no_vuln_content"
    return "ok"


def submit_targets(raw_targets: list, authorized: bool) -> tuple:
    """接收目标入队。返回 (ok, message, hosts)。"""
    if not authorized:
        return False, "必须确认授权（authorized=true）", []
    hosts = []
    for raw in raw_targets:
        host = host_of(raw)
        if not host or not re.match(r"^[a-z0-9][a-z0-9.\-]*$", host):
            continue
        if "." not in host and "localhost" != host:
            continue  # 无 TLD 且非 localhost → 拒绝裸串
        hosts.append((host, raw.strip()))

    if not hosts:
        return False, "无合法目标（需要域名/IP，如 example.com 或 https://x.com）", []

    with _STATE_LOCK:
        state = load_state()
        for host, raw in hosts:
            entry = state["targets"].setdefault(host, {"host": host})
            # 已 done/timeout/failed 的允许重跑；running/pending 跳过
            if entry.get("status") in ("running", "pending"):
                continue
            entry["base_url"] = raw
            entry["status"] = "pending"
            _QUEUE.append(host)
        save_state(state)

    return True, f"已入队 {len(hosts)} 个目标", [h for h, _ in hosts]


def scheduler_loop(max_workers: int, max_turns: int, timeout_s: int,
                   claude_bin: str = DEFAULT_CLAUDE_BIN) -> None:
    """后台调度：从队列取 host，交线程池执行。"""
    pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="batch-worker")
    try:
        while not _STOP.is_set():
            host = None
            with _STATE_LOCK:
                if _QUEUE:
                    host = _QUEUE.pop(0)
            if host is None:
                time.sleep(1)
                continue
            state = load_state()
            entry = state["targets"].get(host, {})
            base_url = entry.get("base_url") or host
            pool.submit(run_worker, host, base_url, max_turns, timeout_s, claude_bin)
    finally:
        pool.shutdown(wait=False)


# ── HTTP ──
def _read_static(name: str) -> bytes:
    path = os.path.join(STATIC_DIR, name)
    if not os.path.exists(path):
        return b"<h1>404</h1>"
    with open(path, "rb") as f:
        return f.read()


def _status_payload() -> dict:
    state = load_state()
    targets = {}
    for host, entry in state["targets"].items():
        targets[host] = {
            "status": entry.get("status", "unknown"),
            "base_url": entry.get("base_url", host),
            "started_at": entry.get("started_at", ""),
            "updated_at": entry.get("updated_at", ""),
            "duration_s": entry.get("duration_s"),
            "error": entry.get("error", ""),
            "exit_code": entry.get("exit_code"),
            "report_validation": entry.get("report_validation", ""),
        }
    return {"version": 1, "queue_len": len(_QUEUE), "active": list(_ACTIVE.keys()),
            "targets": targets}


def _events_tail(host: str, limit: int = DEFAULT_EVENTS_TAIL) -> list:
    path = os.path.join(LOGS_DIR, f"{_safe_host(host)}.jsonl")
    if not os.path.exists(path):
        return []
    lines = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    tail = lines[-limit:]
    out = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
            out.append(evt)
        except json.JSONDecodeError:
            out.append({"type": "raw", "text": line[:500]})
    return out


def _report_text(host: str) -> dict:
    path = os.path.join(RESULTS_DIR, _safe_host(host), "report.md")
    if not os.path.exists(path):
        return {"exists": False, "text": ""}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {"exists": True, "text": "", "error": "read failed"}
    return {"exists": True, "text": text}


class Handler(BaseHTTPRequestHandler):
    server_version = "XiangXiongBatch/1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s\n" % (fmt % args))

    def _send_json(self, obj: dict, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str, content_type: str):
        body = _read_static(name)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send_static("index.html", "text/html; charset=utf-8")
        elif path == "/new.html":
            self._send_static("new.html", "text/html; charset=utf-8")
        elif path == "/report.html":
            self._send_static("report.html", "text/html; charset=utf-8")
        elif path == "/api/status":
            self._send_json(_status_payload())
        elif path.startswith("/api/target/"):
            parts = path.split("/")
            if len(parts) >= 5 and parts[4] == "events":
                host = parts[3]
                self._send_json({"host": host, "events": _events_tail(host)})
            elif len(parts) >= 5 and parts[4] == "report":
                host = parts[3]
                self._send_json({"host": host, ** _report_text(host)})
            else:
                self._send_json({"error": "unknown target api"}, 404)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/api/targets":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json({"ok": False, "error": "bad json"}, 400)
            return
        raw_targets = payload.get("targets", [])
        authorized = bool(payload.get("authorized", False))
        ok, msg, hosts = submit_targets(raw_targets, authorized)
        self._send_json({"ok": ok, "message": msg, "hosts": hosts},
                        200 if ok else 400)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="批量渗透调度器 + 仪表盘")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--claude-bin", default=os.environ.get("XIANGXIONG_CLAUDE") or DEFAULT_CLAUDE_BIN,
                    help="claude 可执行路径（默认 'claude'，可用 XIANGXIONG_CLAUDE 环境变量覆盖）")
    ap.add_argument("--add", nargs="+", help="CLI 直接添加目标（须另加 --authorized）")
    ap.add_argument("--authorized", action="store_true", help="确认已获授权（--add 时必需）")
    args = ap.parse_args()

    if args.add:
        ok, msg, hosts = submit_targets(args.add, args.authorized)
        print(msg, file=sys.stdout)
        print("hosts:", ", ".join(hosts), file=sys.stdout)
        if not ok:
            sys.exit(1)
        return

    sched = threading.Thread(
        target=scheduler_loop,
        args=(args.concurrency, args.max_turns, args.timeout, args.claude_bin),
        daemon=True,
    )
    sched.start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[batch_server] http://127.0.0.1:{args.port}  (concurrency={args.concurrency}, "
          f"max_turns={args.max_turns}, timeout={args.timeout}s, claude_bin={args.claude_bin})", file=sys.stdout)
    print("[batch_server] state: " + STATE_PATH, file=sys.stdout)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[batch_server] shutting down...", file=sys.stdout)
        for proc in list(_ACTIVE.values()):
            _kill_tree(proc)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
