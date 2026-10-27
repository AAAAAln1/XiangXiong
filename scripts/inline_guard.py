#!/usr/bin/env python3
"""inline_guard.py — PreToolUse 守卫：超长内联 Python 必须先落盘。

为什么存在：CLAUDE.md 早有「命令/脚本先落 results/<target>/scripts/，禁止
python - <<EOF 内联」，但纯提示词无机械兜底——日志实测 heredoc 572 次
（平均 1745 字符，31 个签名重发 ≥3 次，单个 pcap 分析重发 43 次），
超长 python -c 491 次。内联无文件身份、改版即重发全文、stdout 永留上下文。

本守卫**不禁止跑这些代码，只要求先落盘**（scapy/dotnet/一次性分析同样适用）。

两种形态（与日志里的真实写法对齐）：
  python - <<'PY' / python3 - <<"EOF" / python - <<- ENDPY …载荷… PY
  python -c "<载荷>" / python3 -c '...'
触发：载荷（去引号/去 heredoc 包裹后）> 400 字符（实测分布断点）。

模式（env XIANGXIONG_INLINE_GUARD=warn|deny，默认 warn）：
- warn：stderr 给 WARN（不拦），追加一行 JSONL 到 results/_inline_guard.log
  （只有长度/形态/120 字符命令前缀，不记载荷全文）；
- deny：返回 hookSpecificOutput.deny + 两条可直接照做的替代动作
  （照 memory_guard 的「deny reason 必须是当前可用的路」范式）。

设计纪律：fail-open（坏输入/异常一律放行，不能因守卫挂了堵死 Bash）；
runtime 工具段豁免（hook_patterns 单一实现）；纯正则，不执行命令、不做语义分析。
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "runtime"))
from hook_patterns import RUNTIME_CALL_RE, SHELL_TOOLS, split_segments  # noqa: E402

THRESHOLD = 400
LOG_PATH = os.path.join(PROJECT_ROOT, "results", "_inline_guard.log")
MODE = os.environ.get("XIANGXIONG_INLINE_GUARD", "warn").strip().lower()

# 引号形态覆盖：'PY' / "PY" / PY / PowerShell 转义的 \"PY\"（反斜杠+引号各若干）
HEREDOC_RE = re.compile(
    r"python[0-9.]*\s+-\s+<<-?\s*[\\'\"]*([A-Za-z_]\w*)[\\'\"]*", re.IGNORECASE)
PYC_RE = re.compile(r"python[0-9.]*\s+-c\b", re.IGNORECASE)
_TARGET_RE = re.compile(r"results[/\\]([^/\\\s]+)", re.IGNORECASE)
# heredoc 接收方是 runtime 工具（如 `python runtime/merge.py - <<EOF` 走合法 stdin）→ 豁免。
_RUNTIME_HEAD_RE = re.compile(r"runtime[/\\][\w.]+\.py", re.IGNORECASE)


def _chain_prefix(command: str, pos: int) -> str:
    """取 heredoc 起点所属链段（&&/||/; 分隔；**不**按换行切——heredoc 本体跨多行）。"""
    line_start = command.rfind("\n", 0, pos) + 1
    cuts = [command.rfind(sep, 0, pos) for sep in ("&&", "||", ";")]
    start = max(line_start, max(cuts) + 2)
    return command[start:pos]


def _heredoc_payload(command: str):
    """在**整条命令**上找最长非 runtime heredoc 载荷（无闭合符按到文末，保守多拦）。"""
    longest = None
    for match in HEREDOC_RE.finditer(command):
        if _RUNTIME_HEAD_RE.search(_chain_prefix(command, match.start()) +
                                   command[match.start():match.end()]):
            continue
        terminator = match.group(1)
        tail = command[match.end():]
        end = re.search(r"(?:^|[\n;])\s*" + re.escape(terminator) + r"\s*(?=[\n;]|$)", tail)
        payload = (tail[:end.start()] if end else tail).lstrip("\r\n").strip()
        if longest is None or len(payload) > len(longest):
            longest = payload
    return longest


def _pyc_payload(segment: str):
    """取 -c 后的载荷：剥一层成对引号；裸串按到段尾（保守）。"""
    match = PYC_RE.search(segment)
    if not match:
        return None
    tail = segment[match.end():].lstrip()
    if tail[:1] in ("'", '"'):
        quote = tail[0]
        end = tail.find(quote, 1)
        return tail[1:end if end != -1 else len(tail)]
    return tail.strip()


def detect(segment: str):
    """单段（单行命令）检测：只认 -c。heredoc 跨多行，由 evaluate 在整命令上判。"""
    payload = _pyc_payload(segment)
    if payload is not None and len(payload) > THRESHOLD:
        return "pyc", len(payload)
    return None


def detect_heredoc(command: str):
    payload = _heredoc_payload(command)
    if payload is not None and len(payload) > THRESHOLD:
        return "heredoc", len(payload)
    return None


def _target_hint(command: str) -> str:
    m = _TARGET_RE.search(command)
    return m.group(1) if m else "<target>"


def deny_reason(command: str, shape: str, length: int) -> str:
    target = _target_hint(command)
    return (
        f"[INLINE GUARD] 拦截超长内联 Python（{shape}，载荷 {length} 字符 > {THRESHOLD}）。\n"
        "内联无文件身份、改版即重发全文、stdout 永留对话；这不是禁止你跑，是要求先落盘：\n"
        f"  1) Write：results/{target}/scripts/<name>.py（迭代用 Edit 改同一文件，别重发全文）\n"
        f"  2) 执行：python runtime/execute.py {target} --purpose \"<why>\" "
        f"--cmd \"python results/{target}/scripts/<name>.py\"\n"
        "一次性分析（pcap/反汇编等）同样落盘；短 python -c（≤400 字符）不拦截。"
    )


def warn_message(command: str, shape: str, length: int) -> str:
    return (
        f"[INLINE GUARD WARN，当前仅提示] 超长内联 Python（{shape}，{length} 字符）；"
        "下次请先 Write 到 results/<target>/scripts/ 再用 execute.py 跑（deny 模式将直接拦截）。"
    )


def _log(command: str, shape: str, length: int) -> None:
    """WARN 观察样本：只记时间/形态/长度/命令哈希，**不记命令原文与载荷**。"""
    try:
        import hashlib
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "shape": shape, "length": length,
            "cmd_sha1": hashlib.sha1(command.encode("utf-8", "replace")).hexdigest()[:12],
        }
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # fail-open：日志写不进不影响判定


def evaluate(command: str):
    """扫描整条命令。heredoc 跨多行在整命令上判（runtime 接收方豁免）；
    -c 按链段判（runtime 段豁免）。返回 (shape, length, context)|None。"""
    heredoc = detect_heredoc(command)
    if heredoc:
        return heredoc[0], heredoc[1], command
    for segment in split_segments(command):
        if not segment.strip() or RUNTIME_CALL_RE.search(segment):
            continue
        found = detect(segment)
        if found:
            return found[0], found[1], segment
    return None


def main() -> None:
    try:
        hook = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return  # fail-open
    if hook.get("tool_name") not in SHELL_TOOLS:
        return
    command = (hook.get("tool_input") or {}).get("command", "")
    if not command:
        return
    try:
        found = evaluate(command)
    except Exception:
        return  # 守卫自身任何异常都不得堵死 Bash
    if not found:
        return
    shape, length, _ = found
    if MODE == "deny":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": deny_reason(command, shape, length),
            }
        }, ensure_ascii=False))
        return
    # 默认 warn：出声 + 留统计样本，不拦
    print(warn_message(command, shape, length), file=sys.stderr)
    _log(command, shape, length)


if __name__ == "__main__":
    main()
