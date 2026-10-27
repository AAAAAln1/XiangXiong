#!/usr/bin/env python3
"""
http_request.py — 黑盒 HTTP 请求工具（瞎子）。

三不原则：
  CAN:   发请求、算长度、截断、写文件、返回 [META] + [BODY]
  CAN:   解析 curl -w 的物理量 (http_code, size_download)
  CANNOT: 判断状态码含义或提取、解释响应字段
  CANNOT: 出现基于响应内容或状态码的业务判断

输出协议：
  [META]   Status: {code} | Length: {bytes} (Truncated to 2048) | Saved: responses/req_NNN.txt
  [BODY]   {截断后的安全内容}

用法：
  python runtime/http_request.py <target> [--experiment-id exp-NNN] [--session <name>] <curl_args...>

会话保持：
  --session <name>  把 Set-Cookie 自动写入 results/<target>/sessions/<name>.jar，
                    后续带同名的请求自动附带该 jar 里的 cookie（curl -b/-c）。
                    模型只需在"登录后接口"的请求里带上 --session <name> 即可复用登录态。
                    不传 --session 时保持无状态（与旧行为一致）。
                    [EXECUTION] 会显示 session: <name> (N cookies)，N=jar 内已存 cookie 数。

契约自述（`--help` 输出同一份文本，见 HELP_TEXT）：
  本工具的**唯一职责**是"发一次请求 + 把证据完整落盘"。因此它自己拥有
  `-s -k -o <证据文件> -w <物理量>` 这组参数 —— 调用方再传同名的 curl 选项会
  覆盖它们，导致**证据文件 0 字节 / status 字段变成你自己的格式串**。
  实测（2026-09-22~24，4 个目标）：14%–57% 的请求保存体为空，全部源自
  `-o nul` / `-o /dev/null` / `-w "S:%{http_code}..."` 这类习惯写法。
  现在这些选项会被**忽略并提示**（见 CAPTURE_BREAKING_OPTIONS），
  调用方不需要为了"保住证据"去改自己的习惯。
"""

import collections
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

try:
    from .experiment import append_observation, resolve_experiment_id
    from .filelock import locked as _file_locked
    from .paths import safe_name as safe_target
    from . import scope as _scope
    from . import turn_state
except ImportError:
    from experiment import append_observation, resolve_experiment_id
    from filelock import locked as _file_locked
    from paths import safe_name as safe_target
    import scope as _scope
    import turn_state

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.environ.get("XIANGXIONG_RESULTS_DIR") or os.path.join(PROJECT_ROOT, "results")
RESPONSES_SUBDIR = "responses"
SESSIONS_SUBDIR = "sessions"
MAX_BODY_CHARS = 2048
# 2026-09-25 速度修复：15→5。实测 000 超时长尾是单会话最大纯等待
# （c63bb02b: 128 个 000 × 15s ≈ 28 分钟 = 其活跃时间 40%；全窗口 38m）。
# HTTP 接口正常响应 <1s，5s 足以区分"慢"与"死"；真慢场景（大文件下载）自行 --max-time 覆盖。
# 超时不重试：000 大多是真死路径，重试 = 双倍惩罚。
CURL_TIMEOUT = 5
SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization", "set-cookie", "token", "x-api-key", "x-auth-token"}

# ── 会破坏证据捕获的 curl 选项（调用方习惯写法 vs 工具职责冲突）───────────────
# 根因：run_curl 固定先传 `-o <证据文件> -w <物理量>`，调用方参数追加在后，两种覆盖各不相同
# （curl 8.13 实测）：
#   - `-w` **last-wins** → status 字段解析出调用方的格式串（如 `S:403 L:0...`），
#     metadata.status 被永久污染，还会被写进实验观察、制造假差分；
#   - `-o` **first-wins** → 调用方的 `-o <path>` **从不生效**、指定路径不会生成文件，
#     但没有任何提示，读者以为文件在别处（实测会话里出现过仓库根下名为 `-o` 的响应残骸）。
#   - `-I` 直接走 HEAD，没有 body 可捕获。
# 处理：带值选项整对丢弃并逐条 NOTE；`-I/--head` → `-X HEAD`（保语义、保捕获）。
# 粘连形态（`-w%{...}` / `-Df` / `-oFILE`）由 ATTACHED_SHORT_RE 一并覆盖 —— 少了它，
# 上面的污染就静默复发（2026-09-25 审计实测）。
CAPTURE_BREAKING_OPTIONS_WITH_VALUE = (
    "-o", "--output", "--output-dir", "-D", "--dump-header",
    "-w", "--write-out", "--trace", "--trace-ascii", "--trace-config",
    "--stderr", "-K", "--config",
)
CAPTURE_BREAKING_OPTIONS_FLAG = ("-#", "--progress-bar", "-O", "--remote-name", "--remote-name-all")
# 粘连短选项：-o/-D/-w/-K 后面直接跟值（curl 接受这种写法）。
ATTACHED_SHORT_RE = re.compile(r"^-[oDwK][^-]")

# 这些 curl 参数把**帮助/版本文本打到 stdout**，会被 -w 解析成 status → 污染证据。
# 必须在**任意位置**拦截（含 `<target> --help`，实测该形态曾把 curl 帮助写进 status）。
CURL_STDOUT_NOISE = ("--help", "-h", "--version", "-V", "--manual")

HELP_TEXT = """\
http_request.py — 黑盒 HTTP 请求工具（证据捕获器）

用法:
  python runtime/http_request.py <target> [选项] <curl 参数...>

选项（工具自己的，放在 curl 参数之前或之后都行）:
  --experiment-id exp-NNN   本次请求挂到指定实验；省略则自动挂当前 active 实验
  --session <name>          cookie jar: results/<target>/sessions/<name>.jar（自动 -b/-c）
  --worker <id>             实验槽隔离标识：并行探索者各用自己的槽，互不抢 .active
  --cred c-NNN[:Header]     凭据引用注入（值不进命令行回显/sidecar）；可多次。
                            默认按 credentials 的 kind 选头：token→Authorization（自动补
                            Bearer 前缀），key→X-API-Key；pw/ntlm/hash 须显式 :头名。
  --sweep 'URL带{a}[/{b}]'  模板化批量枚举（路径/参数值笛卡尔积），见下方 sweep 节
  --help                    显示本帮助（不会发请求）

curl 参数:
  原样透传（-X -d -H -F --max-time --data-binary -G ...）。
  下列"会破坏证据捕获"的选项会被**忽略并提示**（你不需要为了保住证据改习惯）:
    -o/--output  -I/--head（转 -X HEAD）  -D/--dump-header  -w/--write-out
    --trace*  --stderr  -K/--config  --output-dir  -O/--remote-name

工具固定行为（不要重复给）:
  自动加 -s -k；--max-time 默认 5s（真慢场景自行 --max-time 覆盖）
  批量（≥5 条**已确定**的请求行）: --batch <file|->（- = 从 stdin 读，每行一条，# 注释），
    单进程连发（单发 1.5-2.5s/发，批量 <0.5s/发）；[--delay 0.05] 控速；
    回执一行一发 + 末尾非 200/404 差分候选汇总。
  sweep（模板化枚举，路径/参数值笛卡尔积）:
    --sweep 'https://host/api/{a}/{b}' -w a:admin,auth,config -w b:list,status
    [--wordlist a:词表文件] [--delay 0.1] [--rerun]
    {a} 可出现在 URL 与 -d/--data* 值里；-H 头值不插值。展开上限 5000 发。
    自动 resume（中断后重跑只发差额；同模板已跑完默认不重发，加 --rerun 全量重跑）；
    全量结果落 results/<target>/sweeps/<指纹>.tsv，末尾给 code/长度/title 差分矩阵——
    机械差分不是漏洞，异常簇逐条 Read 存盘体、建实验后才许入 surface/vuln。

证据: 每次请求落盘两份 — responses/req_NNN.txt（完整响应，含响应头）+ req_NNN.json（元数据）

输出协议:
  [EXECUTION] return_code / duration / target_contact / session(cookies N->M) / error
  [REQUEST]   method / url
  [RESPONSE]  status / content_type / title / length(N bytes) / saved(responses/req_NNN.txt)
  [BODY]      响应体前 2048 字符（完整内容见 saved 文件）

引用证据: 记账里写 (req_007) —— 编号就是保存的文件名，用 grep/rg 或 Read 直接看。
"""


def strip_capture_breaking_args(args: list) -> tuple:
    """剔除会覆盖工具自身 `-o/-w` 的 curl 选项。返回 (干净 args, 提示列表)。

    机械替换，不判断意图：
      - 带值选项（-o/-w/-D/--trace…）连同它的值一起丢弃
      - `-I/--head` → `-X HEAD`（保持"要 HEAD"的语义，同时保住 -o 捕获）
      - 无值选项（-#/-O）直接丢弃
    每一项都产生一条面向调用方的 NOTE（不静默 —— 静默丢失比丢弃更糟）。
    """
    clean, notes = [], []
    index = 0
    while index < len(args):
        arg = args[index]
        matched_with_value = None
        if arg in CAPTURE_BREAKING_OPTIONS_WITH_VALUE:
            matched_with_value = arg
            value = args[index + 1] if index + 1 < len(args) else ""
            notes.append(f"忽略 {arg} {value[:40]}（证据已由 req_NNN 落盘）")
            index += 2
            continue
        for option in CAPTURE_BREAKING_OPTIONS_WITH_VALUE:
            if option.startswith("--") and arg.startswith(option + "="):
                matched_with_value = option
                notes.append(f"忽略 {option}=（证据已由 req_NNN 落盘）")
                index += 1
                break
        if matched_with_value:
            continue
        if arg in ("-I", "--head"):
            clean.extend(["-X", "HEAD"])
            notes.append("把 -I/--head 换成 -X HEAD（保持语义，同时保住证据文件）")
            index += 1
            continue
        if arg in CAPTURE_BREAKING_OPTIONS_FLAG:
            notes.append(f"忽略 {arg}（证据已由 req_NNN 落盘）")
            index += 1
            continue
        # 粘连形态：-w%{http_code} / -Dhdr / -oFILE / -Kcfg —— 不拦就会静默覆盖工具的 -w
        #（last-wins）重新制造 status 污染（2026-09-25 审计实测）。
        if ATTACHED_SHORT_RE.match(arg):
            notes.append(f"忽略粘连选项 {arg[:20]}（证据已由 req_NNN 落盘）")
            index += 1
            continue
        clean.append(arg)
        index += 1
    return clean, notes

# 跨进程并发互斥：主代理 + 嵌套 deepdive 可同时发请求，
# 编号分配必须原子，否则并发进程读到同一 max 号 → 写同名文件互相覆盖。
# 锁实现见 runtime/filelock.py（与 experiment 的实验编号锁共用单一实现）。
# 函数动态求值（与 turn_state._lock_path 同构）：模块级常量在 import 时绑定
# RESULTS_DIR，测试 monkeypatch RESULTS_DIR 后锁仍指向真实 results/（2026-10-07 核验缺陷）。
def _req_lock_path() -> str:
    return os.path.join(RESULTS_DIR, ".req_lock")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir(target: str) -> str:
    path = os.path.join(RESULTS_DIR, safe_target(target), RESPONSES_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def next_request_number(save_dir: str) -> int:
    with _file_locked(_req_lock_path()):
        highest = 0
        for name in os.listdir(save_dir):
            match = re.fullmatch(r"req_(\d+)\.(?:txt|json)", name)
            if match:
                highest = max(highest, int(match.group(1)))
        number = highest + 1
        # 占位：分配后立即创建元数据文件（内容后续覆盖），使并发进程在真实
        # 写入完成前也能看到该编号已占用，避免撞号重算。
        placeholder = os.path.join(save_dir, f"req_{number:03d}.json")
        if not os.path.exists(placeholder):
            with open(placeholder, "w", encoding="utf-8") as file:
                file.write("{}")
        return number


def relative_result_path(target: str, path: str) -> str:
    target_root = os.path.join(RESULTS_DIR, safe_target(target))
    return os.path.relpath(path, target_root).replace("\\", "/")


def extract_experiment_id(args: list) -> tuple:
    remaining = []
    experiment_id = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--experiment-id":
            if index + 1 >= len(args):
                raise ValueError("--experiment-id 缺少值")
            experiment_id = args[index + 1]
            index += 2
            continue
        if arg.startswith("--experiment-id="):
            experiment_id = arg.split("=", 1)[1]
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return experiment_id, remaining


def extract_session(args: list) -> tuple:
    """从 args 中提取 --session <name>。返回 (session_name, remaining_args)。"""
    remaining = []
    session_name = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--session":
            if index + 1 >= len(args):
                raise ValueError("--session 缺少值")
            session_name = args[index + 1]
            index += 2
            continue
        if arg.startswith("--session="):
            session_name = arg.split("=", 1)[1]
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return session_name, remaining


def extract_worker(args: list) -> tuple:
    """从 args 中提取 --worker <id>。返回 (worker, remaining_args)。

    worker 是**实验槽隔离标识**：并行探索者各带自己的 worker（例如它的 intent ID
    `h:fe2510`），实验与请求归因就走各自的槽，不再抢同一个 `.active`。
    不传时行为与旧版完全一致（用全局 active 槽）。
    """
    remaining = []
    worker = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--worker":
            if index + 1 >= len(args):
                raise ValueError("--worker 缺少值")
            worker = args[index + 1]
            index += 2
            continue
        if arg.startswith("--worker="):
            worker = arg.split("=", 1)[1]
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return worker, remaining


def extract_batch(args: list) -> tuple:
    """提取 --batch <file> 与 --delay <sec>。返回 (batch_file, delay, remaining)。

    批量 = 单进程连发：省掉每请求一次 Python 冷启动+锁+双写（实测单发
    1.5-2.5s/发、批量 <0.5s/发；c6d8fa59 54% 请求间隔 1-3s 全是单发税）。
    """
    remaining = []
    batch_file = None
    delay = 0.05
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--batch":
            if index + 1 >= len(args):
                raise ValueError("--batch 缺少文件路径")
            batch_file = args[index + 1]
            index += 2
            continue
        if arg.startswith("--batch="):
            batch_file = arg.split("=", 1)[1]
            index += 1
            continue
        if arg == "--delay":
            if index + 1 >= len(args):
                raise ValueError("--delay 缺少数值")
            delay = float(args[index + 1])
            index += 2
            continue
        if arg.startswith("--delay="):
            delay = float(arg.split("=", 1)[1])
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return batch_file, delay, remaining


_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}


def _lines_from_source(source: str) -> list:
    """读批量请求行：文件路径，或 "-" 表示 stdin。# 注释/空行跳过。

    2026-10-07 P2：新增 stdin 形态——旧版必须先写批文件，是 --batch 采用率 0.3%
    的一半根因（另一半是只执行不聚合，由 sweep 的差分矩阵解决）。"""
    import shlex  # noqa: F401（仅保持调用方环境一致；解析在 _parse_request_line）
    if source == "-":
        text = sys.stdin.read()
    else:
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _parse_request_line(line: str) -> list:
    """一行 curl 参数串 → 参数列表；行首裸方法词（GET url）→ -X GET。

    不转的话 curl 把 "GET" 当主机名解析 → DNS 失败 status=000（实测踩坑）。"""
    import shlex
    args = shlex.split(line)
    if args and args[0].upper() in _HTTP_METHODS:
        args = ["-X", args[0].upper()] + args[1:]
    return args


def _precheck_scope_lines(target: str, lines: list):
    """整批预检：任一越界，整批拒（避免部分发包）。

    返回 [(序号, 原文, 参数list, url), ...]；解析失败行的参数为 None（留给执行期计数）。
    抛出 ScopeRejected 时 message 带行号。"""
    parsed = []
    for i, line in enumerate(lines, 1):
        try:
            args = _parse_request_line(line)
        except ValueError:
            parsed.append((i, line, None, None))
            continue
        _, url = extract_method_url(normalize_curl_args(args))
        parsed.append((i, line, args, url))
    for i, line, args, url in parsed:
        if args is None:
            continue
        enforce_scope_url(target, url)
    return parsed


def _execute_request_args(target, args, *, experiment_id=None, session_name=None,
                          worker=None, extra_headers=None, extra_notes=None):
    """单条已解析参数 → run_curl（sweep/batch/单发共享这唯一执行入口）。

    extra_headers: [["-H", "Name: value"], ...]（cred 注入），在 strip 之后并入；
    返回 run_curl 的 result dict。"""
    line_args, notes = strip_capture_breaking_args(list(args))
    if extra_headers:
        line_args.extend(h for pair in extra_headers for h in pair)
    if extra_notes:
        notes = list(notes) + list(extra_notes)
    if "-k" not in line_args and "--insecure" not in line_args:
        line_args.insert(0, "-k")
    return run_curl(target, normalize_curl_args(line_args), experiment_id,
                    session_name, worker=worker, arg_notes=notes)


def _execute_rows(target, rows, delay, *, experiment_id=None, session_name=None,
                  worker=None, extra_headers=None, cred_note=None, label="batch",
                  progress=True):
    """逐行执行（rows = [(seq, total, raw_or_desc, args|None)], args=None 表示解析失败）。

    纯机械执行 + 计数，不打印汇总语义（汇总由调用方按 batch/sweep 各自渲染）。
    返回 (counts, records)；records = [{seq,status,method,url,length,title,filename}]。"""
    counts = collections.Counter()
    records = []
    total = len(rows)
    started = time.time()
    for idx, (seq, _, desc, args) in enumerate(rows, 1):
        if args is None:
            print(f"[{seq}/{total}] 解析失败 | {str(desc)[:80]}", flush=True)
            counts["parse_error"] += 1
            continue
        try:
            result = _execute_request_args(
                target, args, experiment_id=experiment_id, session_name=session_name,
                worker=worker, extra_headers=extra_headers,
                extra_notes=[cred_note] if (cred_note and idx == 1) else None)
        except ScopeRejected:
            raise  # 预检漏网（DNS 时序差）时整批停，调用方统一回执
        except Exception as exc:  # 单发失败不中断整批
            print(f"[{seq}/{total}] 异常: {str(exc)[:80]}", flush=True)
            counts["exception"] += 1
            continue
        status = str(result.get("status_code", "000"))
        counts[status] += 1
        if progress:
            print(f"[{seq}/{total}] {status} {result.get('method', '?')} "
                  f"{result.get('url', '?')} → {result.get('filename', '')}", flush=True)
        records.append({
            "seq": seq, "status": status, "method": result.get("method", "?"),
            "url": result.get("url", "?"), "length": _safe_int(result.get("body_length")),
            "title": result.get("title", ""), "filename": result.get("filename", ""),
        })
        if delay and idx < total:
            time.sleep(delay)
    counts["_elapsed"] = round(time.time() - started, 1)
    return counts, records


def _safe_int(value, default=0):
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def run_batch(target: str, batch_file: str, delay: float,
              experiment_id: str = None, session_name: str = None, worker: str = None,
              extra_headers=None, cred_note=None) -> int:
    """批量执行：文件（或 - = stdin）每行一条 curl 参数串，单进程循环 run_curl。

    每发的编号/锁/证据双文件/实验归因/arg_notes 与单发完全一致（同一 run_curl 路径）。
    回执刻意**不含 [BODY]**：一行一发只报 status+URL+存盘路径，末尾给
    「非 200/404 差分候选」清单——那才是值得回读存盘体深挖的行。
    """
    try:
        lines = _lines_from_source(batch_file)
    except OSError as exc:
        print(f"[batch] 读不到文件 {batch_file}: {exc}")
        return 2
    if not lines:
        print(f"[batch] {batch_file} 里没有有效行（每行一条 curl 参数串，# 开头是注释）")
        return 2

    try:
        parsed = _precheck_scope_lines(target, lines)
    except ScopeRejected as exc:
        print(f"[batch] SCOPE GATE：整批未执行。越界行：\n{exc}", flush=True)
        return 2

    anchor = f" exp={experiment_id}" if experiment_id else ""
    src = "stdin" if batch_file == "-" else batch_file
    print(f"[batch] {len(lines)} 发 → {src} | delay={delay}s{anchor}", flush=True)
    if cred_note:
        print(cred_note, flush=True)
    rows = [(i, i, line, args) for i, line, args, _ in parsed]
    counts, records = _execute_rows(
        target, rows, delay, experiment_id=experiment_id, session_name=session_name,
        worker=worker, extra_headers=extra_headers, cred_note=cred_note)
    elapsed = counts.pop("_elapsed")

    anomalies = [(r["status"], r["method"], r["url"], r["filename"])
                 for r in records if r["status"] not in ("200", "404")]
    rate = len(lines) / max(elapsed, 0.1)
    status_part = " ".join(f"{s}×{n}" for s, n in counts.most_common())
    print(f"[batch done] {len(lines)} 发 {elapsed:.1f}s（{rate:.1f} 发/s）| {status_part}")
    if anomalies:
        print(f"[batch 差分候选] 非 200/404 共 {len(anomalies)} 条——逐条读存盘体深挖：")
        for status, method, url, filename in anomalies:
            print(f"  {status} {method} {url} → {filename}")
    else:
        print("[batch] 无非 200/404 —— 该族在 status 维度无差分，回 [COVERAGE]/账本换面")
    return 0 if counts["exception"] + counts["parse_error"] < len(lines) else 1


# ── P2：--cred c-NNN 凭据引用注入 ───────────────────────────────────────────

CRED_MAX_VALUE_CHARS = 4096
# kind → 默认头名的机械映射；无机械 HTTP 形态的 kind 必须显式给 :头名。
CRED_DEFAULT_HEADERS = {"token": "Authorization", "key": "X-API-Key"}
CRED_SCHEME_PREFIXES = ("bearer ", "basic ", "token ")


class CredError(Exception):
    """--cred 取值/形态错误（未发包）。"""


def extract_cred(args: list) -> tuple:
    """提取 --cred c-NNN[:Header]（可多次）。返回 (specs, remaining)。"""
    specs, remaining = [], []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--cred":
            if index + 1 >= len(args):
                raise ValueError("--cred 缺少值（形态 c-NNN 或 c-NNN:Header）")
            specs.append(args[index + 1])
            index += 2
            continue
        if arg.startswith("--cred="):
            specs.append(arg.split("=", 1)[1])
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return specs, remaining


def resolve_cred_headers(target: str, specs: list):
    """把 --cred 引用解析成 curl -H 参数。返回 (header_pairs, display_notes)。

    header_pairs = [["-H", "Name: value"], ...]（并入每发请求；sidecar 由 redact_args 掩码）。
    """
    try:
        from credentials import credential_entry, CRED_ID_RE
    except ImportError:
        from .credentials import credential_entry, CRED_ID_RE
    pairs, notes = [], []
    for spec in specs:
        cid, _, explicit = spec.partition(":")
        cid, explicit = cid.strip(), explicit.strip()
        if not CRED_ID_RE.match(cid):
            raise CredError(f"凭据引用必须是 c-NNN 形态，收到 {spec!r}")
        try:
            entry = credential_entry(target, cid)
        except (KeyError, ValueError) as exc:
            raise CredError(f"{exc}；先 python runtime/credentials.py list {target}")
        with open(entry["path"], "r", encoding="utf-8", errors="replace") as handle:
            value = handle.read().strip().strip("\r\n").strip()
        lines = [ln for ln in value.splitlines() if ln.strip()]
        if len(lines) != 1:
            raise CredError(
                f"[cred] {cid} 值文件为 {len(lines)} 非空行；--cred 只接受单行头值"
                "（多行秘密用 --session 的 jar，或显式 -H 落 scripts）")
        value = lines[0]
        if len(value) > CRED_MAX_VALUE_CHARS:
            raise CredError(f"[cred] {cid} 值超 {CRED_MAX_VALUE_CHARS} 字符，拒绝拼进请求")
        if explicit:
            header_name = explicit
        else:
            kind = entry.get("kind", "other")
            if kind not in CRED_DEFAULT_HEADERS:
                raise CredError(
                    f"[cred] {cid} kind={kind} 无机械 HTTP 头形态；显式指定："
                    f"--cred {cid}:Authorization / {cid}:Cookie / {cid}:<头名>")
            header_name = CRED_DEFAULT_HEADERS[kind]
            if header_name == "Authorization" and not value.lower().startswith(CRED_SCHEME_PREFIXES):
                value = "Bearer " + value
        pairs.append(["-H", f"{header_name}: {value}"])
        notes.append(f"cred: {cid} → {header_name}（值不进回显/sidecar）")
    return pairs, notes


# ── P2：--sweep 模板化批量枚举 ──────────────────────────────────────────────

SWEEP_MAX_LINES = 5000
SWEEP_SUBDIR = "sweeps"
SWEEP_DATA_OPTIONS = ("-d", "--data", "--data-raw", "--data-binary")
_SWEEP_VAR_RE = re.compile(r"\{([A-Za-z_]\w*)\}")


def extract_sweep(args: list) -> tuple:
    """解析 --sweep/-w/--wordlist/--rerun。

    返回 (template, inline, wordlists, rerun, remaining)：
      inline    = [(var, "v1,v2"), ...]（同变量多次后合并）
      wordlists = [(var, path), ...]
    """
    template, inline, wordlists, rerun = None, [], [], False
    remaining = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--sweep":
            if index + 1 >= len(args):
                raise ValueError("--sweep 缺少模板（含 {var} 占位的 URL）")
            template = args[index + 1]
            index += 2
            continue
        if arg.startswith("--sweep="):
            template = arg.split("=", 1)[1]
            index += 1
            continue
        if arg in ("-w", "--word"):
            if index + 1 >= len(args):
                raise ValueError("-w 缺少值（形态 var:v1,v2）")
            inline.append(args[index + 1])
            index += 2
            continue
        if arg.startswith("-w") and arg != "-w":
            inline.append(arg[2:].lstrip("="))
            index += 1
            continue
        if arg == "--wordlist":
            if index + 1 >= len(args):
                raise ValueError("--wordlist 缺少值（形态 var:文件路径）")
            wordlists.append(args[index + 1])
            index += 2
            continue
        if arg.startswith("--wordlist="):
            wordlists.append(arg.split("=", 1)[1])
            index += 1
            continue
        if arg == "--rerun":
            rerun = True
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return template, inline, wordlists, rerun, remaining


def _parse_binding(spec: str, what: str) -> tuple:
    if ":" not in spec:
        raise ValueError(f"{what} 绑定形态必须是 var:值（收到 {spec!r}）")
    var, value = spec.split(":", 1)
    var, value = var.strip(), value.strip()
    if not re.fullmatch(r"[A-Za-z_]\w*", var):
        raise ValueError(f"{what} 变量名必须是 [A-Za-z_][A-Za-z0-9_]*（收到 {var!r}）")
    return var, value


def build_sweep_bindings(inline: list, wordlists: list) -> dict:
    bindings = {}

    def add(var, values, source=None):
        bucket = bindings.setdefault(var, [])
        for value in values:
            if value not in bucket:
                bucket.append(value)

    for spec in inline:
        var, value = _parse_binding(spec, "-w")
        # -w 用逗号分隔多值；单个值本身不支持逗号（无法与分隔符区分）→ 走 --wordlist。
        values = [v.strip() for v in value.split(",") if v.strip()]
        if not values:
            raise ValueError(f"-w {var} 没有有效值（形态 -w {var}:v1,v2）")
        add(var, values)
    for spec in wordlists:
        var, path = _parse_binding(spec, "--wordlist")
        try:
            with open(path, encoding="utf-8") as handle:
                values = [ln.strip() for ln in handle
                          if ln.strip() and not ln.lstrip().startswith("#")]
        except OSError as exc:
            raise ValueError(f"--wordlist 读不到 {path}: {exc}")
        add(var, values)
    return bindings


def _template_vars(template: str) -> list:
    """模板中按首次出现顺序排列的变量名。"""
    seen = []
    for name in _SWEEP_VAR_RE.findall(template):
        if name not in seen:
            seen.append(name)
    return seen


def _reject_braces_in_headers(args: list) -> None:
    index = 0
    while index < len(args):
        if args[index] in ("-H", "--header") and index + 1 < len(args):
            if "{" in args[index + 1]:
                raise ValueError("-H 头值不支持 {var} 插值（v1 只插值 URL 与 -d/--data* body）")
            index += 2
            continue
        if args[index].startswith("--header=") and "{" in args[index]:
            raise ValueError("-H 头值不支持 {var} 插值（v1 只插值 URL 与 -d/--data* body）")
        index += 1


def _substitute(text: str, combo: dict) -> str:
    def repl(match):
        return combo[match.group(1)]
    return _SWEEP_VAR_RE.sub(repl, text)


def expand_sweep_rows(template: str, bindings: dict, shared_args: list):
    """笛卡尔展开。返回 (var_order, [(combo, args), ...])；args 为每发完整 curl 参数。"""
    _reject_braces_in_headers(shared_args)
    # URL 模板与共享参数不得各自带 URL（模板即 URL）
    if _template_vars(template) == [] and "{" not in template:
        pass  # 无变量模板无意义但允许（一发 sweep 退化，仍给 resume/矩阵）
    _, shared_url = extract_method_url(normalize_curl_args(shared_args))
    if shared_url:
        raise ValueError("--sweep 模板即 URL；共享 curl 参数里不要再给 URL（去掉位置参数）")
    order = _template_vars(template)
    # body 模板变量（-d/--data* 值里出现的占位符）并入顺序
    index = 0
    data_slots = []
    while index < len(shared_args):
        arg = shared_args[index]
        if arg in SWEEP_DATA_OPTIONS and index + 1 < len(shared_args):
            data_slots.append(("value", index + 1, shared_args[index + 1]))
            index += 2
            continue
        matched = next((opt for opt in ("--data=", "--data-raw=", "--data-binary=")
                        if arg.startswith(opt)), None)
        if matched:
            data_slots.append(("attached", index, matched))
            index += 1
            continue
        index += 1
    for _, _, text in data_slots:
        for name in _template_vars(text):
            if name not in order:
                order.append(name)

    bound = set(bindings)
    if set(order) != bound:
        missing = [v for v in order if v not in bound]
        extra = [v for v in bindings if v not in order]
        problems = []
        if missing:
            problems.append(f"变量未绑定: {missing}（补 -w {missing[0] if missing else ''}:...）")
        if extra:
            problems.append(f"绑定了模板里不存在的变量: {extra}")
        raise ValueError("；".join(problems))
    if not order and not template:
        raise ValueError("--sweep 需要含 URL 的模板")

    import itertools
    combos = []
    for values in itertools.product(*[bindings[v] for v in order]):
        combos.append(dict(zip(order, values)))
    if len(combos) > SWEEP_MAX_LINES:
        raise ValueError(
            f"sweep 展开 {len(combos)} 行 > 上限 {SWEEP_MAX_LINES}；拆成多个 sweep"
            "（减少词段或分子域）")

    rows = []
    for combo in combos:
        args = list(shared_args)
        for kind, slot, text in data_slots:
            if kind == "value":
                args[slot] = _substitute(text, combo)
            else:
                opt = text
                args[slot] = opt + _substitute(args[slot][len(opt):], combo)
        args.append(_substitute(template, combo))
        rows.append((combo, args))
    return order, rows


def sweeps_dir(target: str) -> str:
    path = os.path.join(RESULTS_DIR, safe_target(target), SWEEP_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


_SWEEP_CANON_VALUE_OPTS = {
    "-X", "--request", "-H", "--header", "-d", "--data", "--data-raw",
    "--data-binary", "-F", "--form", "-A", "--user-agent", "-b", "--cookie",
    "-e", "--referer", "-u", "--user", "--max-time", "--connect-timeout",
}


def _canonical_shared_args(args: list) -> list:
    """共享参数的顺序无关规范形（仅用于 sweep 指纹）：带值选项与开关各自排序。

    同一组参数以不同书写顺序出现（-X POST 在前/在后）视为同一 sweep；
    URL 位置参数是模板（不进共享参数），不参与。"""
    pairs, flags = [], []
    index = 0
    while index < len(args):
        arg = args[index]
        attached = next((opt for opt in ("--data=", "--data-raw=", "--data-binary=",
                                         "--header=", "--form=") if arg.startswith(opt)), None)
        if attached:
            pairs.append((attached.rstrip("="), arg[len(attached):]))
            index += 1
            continue
        if arg in _SWEEP_CANON_VALUE_OPTS and index + 1 < len(args):
            pairs.append((arg, args[index + 1]))
            index += 2
            continue
        flags.append(arg)
        index += 1
    return sorted(pairs) + sorted(flags)


def _sweep_fingerprint(template: str, bindings: dict, shared_args: list) -> str:
    canonical = json.dumps({
        "t": template,
        "v": {k: bindings[k] for k in sorted(bindings)},
        "a": _canonical_shared_args(normalize_curl_args(shared_args)),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def _request_sig(args: list) -> str:
    canonical = json.dumps(normalize_curl_args(strip_capture_breaking_args(args)[0]),
                           ensure_ascii=False)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _load_sweep_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and "done" in data:
            return data
    except (OSError, ValueError):
        pass
    return {"done": {}}


def _write_sweep_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _cluster_lengths(lengths: list) -> list:
    """同 (status,title) 组内单链长度聚类：相邻 gap ≤ max(16, 5% 较长者) 合并。"""
    ordered = sorted(lengths)
    clusters = []
    for length in ordered:
        if clusters:
            hi, lo = length, clusters[-1][1]
            if length - clusters[-1][1] <= max(16, round(0.05 * max(length, clusters[-1][1]))):
                clusters[-1][1] = length
                clusters[-1][2] += 1
                continue
        clusters.append([length, length, 1])
    return clusters


def render_sweep_matrix(records: list) -> str:
    """确定性差分矩阵：(status,title) 分组 + 长度单链聚类；基线簇沉底，异常前置。"""
    groups = {}
    for r in records:
        key = (r["status"], r.get("title") or "")
        groups.setdefault(key, []).append(r)

    clusters = []
    for (status, title), rows in groups.items():
        for lo, hi, count in _cluster_lengths([r["length"] for r in rows]):
            members = [r for r in rows if lo <= r["length"] <= hi]
            clusters.append({"status": status, "title": title, "lo": lo, "hi": hi,
                             "count": count, "samples": members[:5]})

    if not clusters:
        return "[sweep 差分矩阵]（无成功响应）\n"
    baseline = max(clusters, key=lambda c: c["count"])

    def priority(c):
        s = c["status"]
        if s and s[0] == "5":
            return 0
        if s in ("401", "403", "405"):
            return 1
        if s == "200":
            return 3
        return 2

    ordered = sorted((c for c in clusters if c is not baseline),
                     key=lambda c: (priority(c), c["status"], c["lo"]))

    def length_text(c):
        return f"len={c['lo']}" if c["lo"] == c["hi"] else f"len={c['lo']}-{c['hi']}"

    lines = ["[sweep 差分矩阵]"]
    for c in ordered:
        urls = ", ".join(os.path.basename("/x" + s["url"].split("?", 1)[0].rstrip("/"))[-40:]
                         for s in c["samples"][:3])
        extra = f", 等{c['count']}条见 TSV" if c["count"] > 3 else ""
        title_part = f" {c['title'][:24]}" if c["title"] else ""
        lines.append(f"  {c['status']}{title_part} {length_text(c)} ({c['count']})  {urls}{extra}")
    pct = baseline["count"] * 100 // len(records)
    lines.append(f"  基线簇 {baseline['status']} {length_text(baseline)} "
                 f"({baseline['count']}, {pct}%)")
    lines.append("机械差分，不是漏洞：异常簇逐条 Read 存盘体验证、建实验后才许入 surface/vuln。")
    return "\n".join(lines) + "\n"


def run_sweep(target: str, template: str, bindings: dict, shared_args: list, delay: float,
              *, experiment_id=None, session_name=None, worker=None,
              extra_headers=None, cred_note=None, rerun=False) -> int:
    var_order, expanded = expand_sweep_rows(template, bindings, shared_args)
    # scope 展开后全量预检：任一越界整批不发（零 req 残骸）
    try:
        for combo, args in expanded:
            _, url = extract_method_url(normalize_curl_args(args))
            enforce_scope_url(target, url)
    except ScopeRejected as exc:
        print(f"[sweep] SCOPE GATE：整批未执行（{len(expanded)} 发均未发出）。\n{exc}")
        return 2

    fp = _sweep_fingerprint(template, bindings, shared_args)
    directory = sweeps_dir(target)
    state_path = os.path.join(directory, f"{fp}.json")
    tsv_path = os.path.join(directory, f"{fp}.tsv")
    total = len(expanded)

    if rerun and os.path.exists(state_path):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        for p in (state_path, tsv_path):
            if os.path.exists(p):
                os.replace(p, f"{p}.done-{stamp}")

    state = _load_sweep_state(state_path)
    state.update({"fp": fp, "template": template, "total": total,
                  "status": "running"})
    done = state.setdefault("done", {})
    pending = [(combo, args) for combo, args in expanded if _request_sig(args) not in done]
    skipped = total - len(pending)

    anchor = f" exp={experiment_id}" if experiment_id else ""
    if skipped:
        print(f"[sweep] fp={fp} 续跑 {len(pending)}/{total}（已有 {skipped} 发）"
              f" → {os.path.join('results', safe_target(target), SWEEP_SUBDIR)}{anchor}",
              flush=True)
    else:
        print(f"[sweep] fp={fp} {total} 发 → {os.path.join('results', safe_target(target), SWEEP_SUBDIR)}"
              f" | delay={delay}s{anchor}", flush=True)
    if cred_note:
        print(cred_note, flush=True)

    if os.path.exists(tsv_path) and not skipped:
        os.remove(tsv_path)

    counts = collections.Counter()
    started = time.time()
    executed = 0
    for combo, args in pending:
        sig = _request_sig(args)
        try:
            result = _execute_request_args(
                target, args, experiment_id=experiment_id, session_name=session_name,
                worker=worker, extra_headers=extra_headers,
                extra_notes=[cred_note] if (cred_note and executed == 0) else None)
        except ScopeRejected as exc:
            print(f"[sweep] SCOPE GATE：已停（预检后变化）。\n{exc}")
            _write_sweep_state(state_path, state)
            return 2
        except Exception as exc:
            counts["exception"] += 1
            print(f"[sweep] 异常: {str(exc)[:80]}", flush=True)
            continue
        status = str(result.get("status_code", "000"))
        counts[status] += 1
        executed += 1
        record = {
            "sig": sig, "req": result.get("filename", "").replace(".txt", ""),
            "status": status, "length": _safe_int(result.get("body_length")),
            "title": result.get("title", ""), "url": result.get("url", "?"),
        }
        done[sig] = {k: v for k, v in record.items() if k != "sig"}
        with open(tsv_path, "a", encoding="utf-8") as handle:
            handle.write(f"{record['req']}\t{status}\t{record['length']}\t"
                         f"{record['title'].replace(chr(9), ' ')}\t{record['url']}\n")
        _write_sweep_state(state_path, state)
        if delay:
            time.sleep(delay)

    records = [{"status": v["status"], "length": v["length"], "title": v.get("title", ""),
                "url": v["url"], "filename": v["req"] + ".txt", "method": "?"}
               for v in done.values()]
    state["status"] = "done"
    state["updated"] = datetime.now(timezone.utc).isoformat()
    _write_sweep_state(state_path, state)

    elapsed = round(time.time() - started, 1) if pending else 0.0
    status_counts = collections.Counter(r["status"] for r in records)
    part = " ".join(f"{s}×{n}" for s, n in sorted(status_counts.items()))
    print(f"[sweep done] {total} 发（本次执行 {executed}）{elapsed}s | {part} · TSV {os.path.basename(tsv_path)}")
    print(render_sweep_matrix(records))
    return 0


def session_jar_path(target: str, session_name: str) -> str:
    """会话 jar 的保存路径。目录与 responses 同级，按 target 隔离。"""
    dir_path = os.path.join(RESULTS_DIR, safe_target(target), SESSIONS_SUBDIR)
    os.makedirs(dir_path, exist_ok=True)
    # 只允许安全的 jar 名，防止路径穿越
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_name)
    return os.path.join(dir_path, f"{safe}.jar")


def count_cookies_in_jar(jar_path: str) -> int:
    """机械统计 jar 文件中的 cookie 行数（跳过 Netscape 头/注释）。

    注意：HttpOnly cookie 行以 `#HttpOnly_` 开头，属于有效行，只有
    `# Netscape HTTP Cookie File` 这类文件头注释才跳过。
    """
    try:
        with open(jar_path, "r", encoding="utf-8", errors="replace") as file:
            count = 0
            for line in file:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#HttpOnly_"):
                    count += 1
                    continue
                if stripped.startswith("#"):
                    continue
                count += 1
            return count
    except (OSError, ValueError):
        return 0


def is_sensitive_header(name: str) -> bool:
    normalized = name.strip().lower()
    return normalized in SENSITIVE_HEADERS or "token" in normalized or "api-key" in normalized


def redact_args(args: list) -> list:
    redacted = list(args)
    index = 0
    while index < len(redacted):
        arg = redacted[index]
        if arg in {"-H", "--header"} and index + 1 < len(redacted):
            header = redacted[index + 1]
            name, separator, _ = header.partition(":")
            if separator and is_sensitive_header(name):
                redacted[index + 1] = f"{name}: [REDACTED]"
            index += 2
            continue
        if arg.startswith("--header="):
            header = arg.split("=", 1)[1]
            name, separator, _ = header.partition(":")
            if separator and is_sensitive_header(name):
                redacted[index] = f"--header={name}: [REDACTED]"
        elif arg in {"-b", "--cookie", "-u", "--user"} and index + 1 < len(redacted):
            redacted[index + 1] = "[REDACTED]"
            index += 1
        elif arg.startswith("--cookie=") or arg.startswith("--user="):
            redacted[index] = arg.split("=", 1)[0] + "=[REDACTED]"
        index += 1
    return redacted


def normalize_curl_args(args: list) -> list:
    normalized = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--method":
            if index + 1 >= len(args):
                raise ValueError("--method 缺少值")
            normalized.extend(["-X", args[index + 1]])
            index += 2
            continue
        if arg.startswith("--method="):
            normalized.extend(["-X", arg.split("=", 1)[1]])
            index += 1
            continue
        if arg == "--timeout":
            if index + 1 >= len(args):
                raise ValueError("--timeout 缺少值")
            normalized.extend(["--max-time", args[index + 1]])
            index += 2
            continue
        if arg.startswith("--timeout="):
            normalized.extend(["--max-time", arg.split("=", 1)[1]])
            index += 1
            continue
        normalized.append(arg)
        index += 1
    return normalized


def extract_method_url(args: list) -> tuple:
    method = None
    url = None
    index = 0
    options_with_value = {
        "-A", "--user-agent", "-b", "--cookie", "-d", "--data", "--data-raw",
        "--data-binary", "-e", "--referer", "-F", "--form", "-H", "--header",
        "-o", "--output", "-u", "--user", "-x", "--proxy", "-X", "--request",
        "--connect-timeout", "--max-time", "-w", "--write-out",
    }
    while index < len(args):
        arg = args[index]
        if arg in {"-X", "--request"} and index + 1 < len(args):
            method = args[index + 1]
            index += 2
            continue
        if arg.startswith("--request="):
            method = arg.split("=", 1)[1]
            index += 1
            continue
        if arg == "--url" and index + 1 < len(args):
            url = args[index + 1]
            index += 2
            continue
        if arg.startswith("--url="):
            url = arg.split("=", 1)[1]
            index += 1
            continue
        if arg in options_with_value:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        url = arg
        index += 1
    if method is None:
        data_prefixes = ("--data=", "--data-raw=", "--data-binary=", "--form=")
        has_data = any(
            arg in {"-d", "--data", "--data-raw", "--data-binary", "-F", "--form"}
            or arg.startswith(data_prefixes)
            for arg in args
        )
        method = "POST" if has_data else "GET"
    return method, url


def write_metadata(path: str, metadata: dict) -> None:
    with open(path, "w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)


class ScopeRejected(Exception):
    """第一层授权闸拒绝（未发包）。message 是可抄回执。"""


def enforce_scope_url(target: str, url: str) -> None:
    """发包前对目标 host 做授权判定（机械硬闸）。

    - 纯公网目标且无 scope：维持原路径（scope 闸只管 engagement/私网）；
    - 字面 IP：按 scope.evaluate；私网 IP 无 scope → 拒；
    - 主机名：先看声明主机名，未声明则 DNS 解析后按私网复核（解析失败不替 curl 决策）。
    """
    if not url:
        return
    host = urlparse(url if "://" in url else "http://" + url).hostname or ""
    if not host:
        return
    # 字面 IPv4/IPv6
    try:
        ipaddress.ip_address(host.strip("[]"))
        ip = host.strip("[]")
        decision = _scope.evaluate(target, ip)
        if decision["decision"] in ("deny", "out_of_scope"):
            raise ScopeRejected(_scope.reject_message(target, ip, decision))
        if decision["decision"] == "no_scope" and _scope.is_private_literal(ip):
            raise ScopeRejected(_scope.reject_message(target, ip, decision))
        return
    except ValueError:
        pass
    # 主机名
    if _scope.has_scope(target):
        ips = _scope.resolve_hostname_ips(host)
        decision = _scope.evaluate_hostname(target, host, ips)
        if decision["decision"] in ("deny", "out_of_scope"):
            raise ScopeRejected(_scope.reject_message(target, host, decision))
        # out-of-scope 但解析出的全是公网：scope engagement 默认不出公网 → 拒
        if decision["decision"] == "out_of_scope" and ips and all(
                not _scope.is_private_literal(ip) for ip in ips):
            raise ScopeRejected(_scope.reject_message(target, host, decision))
        if decision["decision"] == "out_of_scope" and not ips:
            # 无法解析时不替 curl 决策，放行自然失败（诚实边界：DNS 时序差）
            return
    else:
        # 无 scope：主机名解析到私网即拒（防用 DNS 名绕过字面 IP 检查）
        ips = _scope.resolve_hostname_ips(host)
        if any(_scope.is_private_literal(ip) for ip in ips):
            decision = {"decision": "no_scope", "matched": ""}
            raise ScopeRejected(_scope.reject_message(target, host, decision))


def extract_title(body: str) -> str:
    """机械提取 HTML <title> 文本（限首 120 字符）。不解释含义。"""
    match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    title = re.sub(r"<[^>]+>", "", match.group(1))
    title = re.sub(r"\s+", " ", title).strip()
    return title[:120]


def run_curl(target: str, args: list, experiment_id: str = None, session_name: str = None,
             worker: str = None, arg_notes: list = None) -> dict:
    """执行 curl，保存完整响应 body 与机械执行 metadata。

    `arg_notes` 是 strip_capture_breaking_args 的提示（被忽略的 curl 选项），
    原样回显给调用方并记进 metadata —— 丢弃必须可见。
    """
    args = normalize_curl_args(args)
    method, url = extract_method_url(args)
    # 第一层授权闸：必须在任何写副作用之前——不建目录、不分配编号、不建占位、不发包。
    # 2026-10-07 核验修复：旧顺序先 next_request_number（建 {} 占位并消耗编号）后过闸，
    # scope 拒绝会留下 req_NNN.json 残骸且污染编号序列。
    enforce_scope_url(target, url)
    semantic_experiment_id = resolve_experiment_id(target, experiment_id, worker)
    save_dir = ensure_dir(target)
    number = next_request_number(save_dir)
    filename = f"req_{number:03d}.txt"
    metadata_filename = f"req_{number:03d}.json"
    save_path = os.path.join(save_dir, filename)
    metadata_path = os.path.join(save_dir, metadata_filename)
    body_path = save_path + ".body.tmp"
    started = time.time()

    session_jar = None
    if session_name:
        session_jar = session_jar_path(target, session_name)
        # 请求前：jar 里已有的 cookie 数（用于展示会话状态）
        cookie_before = count_cookies_in_jar(session_jar)
        # curl -b <jar> 读会话，-c <jar> 把 Set-Cookie 写回 jar
        curl_args = [
            "curl", "--max-time", str(CURL_TIMEOUT), "-s", "-o", body_path,
            "-w", "%{http_code}|%{size_download}|%{time_total}|%{content_type}",
            "-b", session_jar, "-c", session_jar,
        ]
        if "-k" not in args and "--insecure" not in args:
            curl_args.append("-k")
    else:
        cookie_before = 0
        curl_args = [
            "curl", "--max-time", str(CURL_TIMEOUT), "-s", "-o", body_path,
            "-w", "%{http_code}|%{size_download}|%{time_total}|%{content_type}",
        ]
    curl_args.extend(args)

    status_code = "000"
    body_length = "0"
    duration = 0.0
    content_type = ""
    title = ""
    return_code = None
    stderr = ""
    full_body = ""
    error = None
    try:
        result = subprocess.run(
            curl_args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CURL_TIMEOUT + 5,
        )
        return_code = result.returncode
        stderr = result.stderr or ""
        parts = (result.stdout or "").strip().split("|")
        status_code = parts[0] if parts and parts[0] else "000"
        body_length = parts[1] if len(parts) > 1 and parts[1] else "0"
        try:
            duration = float(parts[2]) if len(parts) > 2 else round(time.time() - started, 3)
        except ValueError:
            duration = round(time.time() - started, 3)
        content_type = parts[3] if len(parts) > 3 else ""
        if return_code != 0:
            detail = stderr.strip() or ("请求超时" if return_code == 28 else "无错误详情")
            error = f"curl 失败 (退出码 {return_code}): {detail}"
    except subprocess.TimeoutExpired as exc:
        duration = round(time.time() - started, 3)
        stderr_value = exc.stderr or ""
        stderr = stderr_value.decode("utf-8", errors="replace") if isinstance(stderr_value, bytes) else stderr_value
        error = f"curl 超时 ({CURL_TIMEOUT}s)"
    except OSError as exc:
        duration = round(time.time() - started, 3)
        stderr = str(exc)
        error = str(exc)

    if os.path.exists(body_path):
        with open(body_path, "r", encoding="utf-8", errors="replace") as file:
            full_body = file.read()
        os.replace(body_path, save_path)
    else:
        with open(save_path, "w", encoding="utf-8", errors="replace") as file:
            file.write("")

    cookie_after = count_cookies_in_jar(session_jar) if session_jar else 0
    title = extract_title(full_body) if content_type.startswith("text/html") else ""
    body_reference = relative_result_path(target, save_path)
    metadata_reference = relative_result_path(target, metadata_path)
    metadata = {
        "target": target,
        "timestamp": now_iso(),
        "args": redact_args(args),
        "method": method,
        "url": url,
        "status": status_code,
        "length": body_length,
        "duration": duration,
        "content_type": content_type,
        "title": title,
        "return_code": return_code,
        "stderr": stderr,
        "response_body": body_reference,
        "experiment_id": semantic_experiment_id,
        "session": session_name,
        "session_cookies_before": cookie_before,
        "session_cookies_after": cookie_after,
        "arg_notes": list(arg_notes or []),
    }
    write_metadata(metadata_path, metadata)

    # F0：登记最近请求编号，供 merge 证据闸 REJECT 回执做「编号存在提示」（非背书）。
    # 位置在 scope 闸（上方 enforce_scope_url，越界先抛）与证据落盘之后；record 自身全静默。
    turn_state.record(target, "req", f"req_{number:03d}")

    association_error = None
    if semantic_experiment_id:
        observation = (
            f"method: {method}\nurl: {url}\nstatus: {status_code}\n"
            f"length: {body_length}\nduration: {duration}s\n"
            f"content_type: {content_type}\ntitle: {title}\n"
            f"body_artifact: {body_reference}\nmetadata_artifact: {metadata_reference}"
        )
        try:
            append_observation(target, semantic_experiment_id, observation, metadata_reference)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            association_error = (
                f"无法关联实验 {semantic_experiment_id}: {exc}; "
                f"请求记录已保存至 {metadata_reference}"
            )

    truncated = len(full_body) > MAX_BODY_CHARS
    return {
        "method": method,
        "url": url,
        "status_code": status_code,
        "body_length": body_length,
        "duration": duration,
        "content_type": content_type,
        "title": title,
        "return_code": return_code,
        "stderr": stderr,
        "truncated": truncated,
        "body": full_body[:MAX_BODY_CHARS],
        "filename": filename,
        "metadata_filename": metadata_filename,
        "error": error,
        "association_error": association_error,
        "session_name": session_name,
        "session_cookies_before": cookie_before,
        "session_cookies_after": cookie_after,
        "arg_notes": list(arg_notes or []),
        "worker": worker,
    }


def format_output(result: dict) -> str:
    """
    输出协议 v2：区分 runtime_error / transport_error / target_response
    
    段落：
      [EXECUTION] — curl 执行状态
      [REQUEST]   — 实际发送的请求摘要
      [RESPONSE]  — 目标响应摘要
      [BODY]      — 响应体
    """
    # 判断 target_contact
    return_code = result.get("return_code", 0)
    status_code = result.get("status_code", "000")
    target_contact = (return_code == 0 and status_code != "000")
    
    # 判断 error_type
    error = result.get("error")
    error_type = None
    if error:
        if "超时" in error or return_code == 28:
            error_type = "transport_error"
        elif return_code == 6:  # DNS
            error_type = "transport_error"
        elif return_code == 7:  # 连接失败
            error_type = "transport_error"
        elif return_code != 0:
            error_type = "runtime_error"
    
    # [EXECUTION]
    execution = [
        "[EXECUTION]",
        f"return_code: {return_code}",
        f"duration: {result.get('duration', 0)}s",
        f"target_contact: {str(target_contact).lower()}",
    ]
    session_name = result.get("session_name")
    if session_name:
        before = result.get("session_cookies_before", 0)
        after = result.get("session_cookies_after", 0)
        delta = after - before
        delta_s = f"{delta:+d}" if delta >= 0 else f"{delta:+d}"
        execution.append(
            f"session: {session_name} (cookies {before} -> {after}{delta_s})"
        )
    if error:
        execution.append(f"error_type: {error_type or 'unknown'}")
        execution.append(f"error_detail: {error}")
    for note in result.get("arg_notes") or []:
        execution.append(f"note: {note}")
    worker = result.get("worker")
    if worker:
        execution.append(f"worker: {worker}")
    
    # [REQUEST]
    request_section = [
        "[REQUEST]",
        f"method: {result.get('method', 'UNKNOWN')}",
        f"url: {result.get('url', 'UNKNOWN')}",
    ]
    
    # [RESPONSE]
    if target_contact:
        response_lines = [
            f"status: {status_code}",
        ]
        content_type = result.get("content_type") or ""
        if content_type:
            response_lines.append(f"content_type: {content_type}")
        title = result.get("title") or ""
        if title:
            response_lines.append(f"title: {title}")
        response_lines.extend([
            f"length: {result['body_length']} bytes",
            f"saved: responses/{result['filename']}",
        ])
        response_section = ["[RESPONSE]"] + response_lines
    else:
        response_section = [
            "[RESPONSE]",
            "(未收到响应)",
        ]
    
    # [BODY]
    if result.get("truncated"):
        body_note = f"(截断到 {MAX_BODY_CHARS} 字符，完整内容见 responses/{result['filename']})"
    else:
        body_note = ""
    
    body_display = result["body"] if result.get("body") else "(空响应体)" if target_contact else "(请求失败)"
    body_section = ["[BODY]"]
    if body_note:
        body_section.append(body_note)
    body_section.append(body_display)
    
    return "\n".join(execution + [""] + request_section + [""] + response_section + [""] + body_section)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    argv = sys.argv[1:]
    # help/版本/手册这类参数把**文本打到 stdout**，会被 -w 解析成 status → 污染证据。
    # 必须在**任意位置**拦截，否则 `http_request.py <target> --help`（更自然的写法）
    # 会把 curl 帮助写进 req_NNN 的 status 字段并进实验观察 —— 正是本工具宣称要修的
    # 那类污染（历史会话 zhihu req_089/090 即此形态，2026-09-25 审计复测仍可复现）。
    if any(arg in CURL_STDOUT_NOISE for arg in argv) or (argv and argv[0] == "help"):
        print(HELP_TEXT)
        sys.exit(0)
    if len(argv) < 2:
        print(HELP_TEXT)
        sys.exit(1)

    target = argv[0]
    if target.startswith("-"):
        print(f"[META]\nStatus: ERROR | Error: 第一个参数必须是 target，收到 '{target}'\n"
              f"[BODY]\n(请求失败：用法见 python runtime/http_request.py --help)")
        sys.exit(1)

    try:
        experiment_id, rest = extract_experiment_id(argv[1:])
        session_name, rest = extract_session(rest)
        worker, rest = extract_worker(rest)
        cred_specs, rest = extract_cred(rest)
        sweep_template, sweep_inline, sweep_wordlists, sweep_rerun, rest = extract_sweep(rest)
        batch_file, batch_delay, rest = extract_batch(rest)
        raw_args, arg_notes = strip_capture_breaking_args(rest)
        curl_args = normalize_curl_args(raw_args)
        if sweep_template and batch_file:
            raise ValueError("--sweep 与 --batch 互斥（sweep=模板展开；batch=现成请求行）")
        cred_headers, cred_notes = (
            resolve_cred_headers(target, cred_specs) if cred_specs else ([], []))
    except ValueError as error:
        print(f"[META]\nStatus: ERROR | Error: {error}\n[BODY]\n(请求失败)")
        sys.exit(1)
    except CredError as error:
        print(f"[META]\nStatus: ERROR | Error: {error}\n[BODY]\n(未发包)")
        sys.exit(2)

    cred_note = cred_notes[0] if cred_notes else None

    # sweep 模式：模板笛卡尔展开 → 预检 → resume 执行 → 差分矩阵
    if sweep_template:
        try:
            bindings = build_sweep_bindings(sweep_inline, sweep_wordlists)
            sys.exit(run_sweep(
                target, sweep_template, bindings, curl_args, batch_delay,
                experiment_id=experiment_id, session_name=session_name, worker=worker,
                extra_headers=cred_headers, cred_note=cred_note, rerun=sweep_rerun))
        except ValueError as error:
            print(f"[META]\nStatus: ERROR | Error: {error}\n[BODY]\n(sweep 未执行)")
            sys.exit(1)
        except ScopeRejected as exc:
            print(str(exc))
            sys.exit(2)

    # 批量模式：文件行即请求，单进程连发（每发的落盘/归因与单发完全同一 run_curl 路径）
    if batch_file:
        if rest and rest not in (["-s"], []):
            print(f"[batch] 注意：批量模式下忽略命令行剩余 curl 参数 {rest}", flush=True)
        try:
            sys.exit(run_batch(target, batch_file, batch_delay,
                               experiment_id=experiment_id, session_name=session_name,
                               worker=worker, extra_headers=cred_headers, cred_note=cred_note))
        except ScopeRejected as exc:
            print(str(exc))
            sys.exit(2)

    if cred_headers:
        curl_args.extend(part for pair in cred_headers for part in pair)
        arg_notes = list(arg_notes) + cred_notes
    if "-k" not in curl_args and "--insecure" not in curl_args:
        curl_args.insert(0, "-k")
    try:
        result = run_curl(target, curl_args, experiment_id, session_name,
                          worker=worker, arg_notes=arg_notes)
    except ScopeRejected as exc:
        print(str(exc))
        sys.exit(2)

    try:
        print(format_output(result))
    except UnicodeEncodeError:
        result["body"] = result.get("body", "").encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        print(format_output(result))
    if result.get("error") or result.get("association_error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
