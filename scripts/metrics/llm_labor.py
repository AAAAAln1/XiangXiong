#!/usr/bin/env python3
"""llm_labor.py — LLM 机械劳动度量（减负改造的采用率验收工具）。

用法：
  python scripts/metrics/llm_labor.py --logs <claude projects 目录> [--since 2026-10-07] [--json]
  python scripts/metrics/llm_labor.py --selfcheck

设计纪律（两轮日志分析的教训，固化进代码）：
1. **样本边界硬编码并打印**：主会话 <dir>/*.jsonl + 子代理 <dir>/*/subagents/*.jsonl，
   缺任一组输出 ⚠（workbuddy 初版只扫主会话，漏掉 2767 次子代理 Bash）。
2. **零值哨兵**：任何「被禁止行为」指标为 0 时标 ⚠——0 可能是检测器漏报
   （heredoc 曾被报 0，实测 572 次），禁止当作好消息。
3. `--selfcheck` 内置正负样本断言，检测器改动必须先过自检。
4. stdlib only，确定性输出；只统计可观测行为，不猜意图。

输出：默认人类摘要；--json 输出机读 JSON（含样本边界与全部计数）。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter

# ── 检测器（全部纯正则，可被 --selfcheck 直接断言）──────────────────────────

_PYTHON_PREFIX = r"python(?:\d+(?:\.\d+)*)?"

# heredoc：python - <<'PY' / python3 - <<"EOF" / python - <<- EOF（分隔符形态多变）
HEREDOC_RE = re.compile(r"" + _PYTHON_PREFIX + r"\s+-\s+<<-?\s*['\"]?([A-Za-z_]\w*)['\"]?")
PYC_RE = re.compile(r"" + _PYTHON_PREFIX + r"\s+-c\b")
FOR_HTTP_RE = re.compile(r"\bfor\b.{0,400}?\bdo\b.{0,4000}?http_request\.py", re.IGNORECASE | re.DOTALL)
HTTP_RE = re.compile(r"http_request\.py", re.I)
SWEEP_RE = re.compile(r"--sweep\b")
BATCH_RE = re.compile(r"--batch(?:=|\s)")
BG_RE = re.compile(r"--background\b")
SESSION_RE = re.compile(r"--session(?:=|\s)")
BEARER_LITERAL_RE = re.compile(r"authorization:\s*bearer\s+[A-Za-z0-9\-\._]{12,}", re.I)
COOKIE_LITERAL_RE = re.compile(r"(?:^|\s)-H\s+['\"]?cookie:\s*[^'\"\s]", re.I)
WINDOW_READ_RE = re.compile(r"\bhead\s+-c\b|\btail\s+-c\b|\bsed\s+-n\b")
EVIDENCE_CALL_RE = re.compile(r"evidence\.py\b")
CRED_CALL_RE = re.compile(r"--cred(?:=|\s)c-\d{3,}")
DELTA_FILE_RE = re.compile(r"merge\.py[^\n]*--delta-file\s+(\S+)")

# python -c 载荷同时触及 json 解析与 responses/req 证据目录 → 手写证据解析
PYC_RESPONSES_PARSE_RE = re.compile(r"json\.(loads|load)|\.json\(\)", re.I)
RESPONSES_TOKEN_RE = re.compile(r"responses[/\\]|req_\d|req_\*|glob\(", re.I)

MERGE_REJECT_RE = re.compile(r"\[merge\]\s*REJECTED")
INLINE_LONG_THRESHOLD = 400


def detect_heredoc(command: str):
    """返回 (匹配数, 超过 400 字符的载荷数, 最长载荷字符数)。

    载荷 = 匹配行之后到行首独立分隔符之间的文本；找不到闭合符按到段尾计（保守）。
    """
    count, long_n, longest = 0, 0, 0
    for match in HEREDOC_RE.finditer(command):
        count += 1
        terminator = match.group(1)
        tail = command[match.end():]
        end_m = re.search(r"(?:^|[\n;])\s*" + re.escape(terminator) + r"\s*(?=[\n;]|$)", tail)
        payload = tail[:end_m.start()] if end_m else tail
        payload = payload.lstrip("\r\n")
        longest = max(longest, len(payload.strip()))
        if len(payload.strip()) > INLINE_LONG_THRESHOLD:
            long_n += 1
    return count, long_n, longest


def pyc_responses_parse(command: str) -> bool:
    """整条命令是否为「用 python -c 手写解析证据目录」的形态。"""
    return bool(PYC_RE.search(command)) and bool(
        PYC_RESPONSES_PARSE_RE.search(command) and RESPONSES_TOKEN_RE.search(command)
    )


# ── jsonl 遍历（工具调用在 content 数组里，结构同 Claude Code transcript）───

def iter_tool_events(obj):
    """产出 (tool_name, tool_input, tool_result_text, is_error)。"""
    msg = obj.get("message") or {}
    content = msg.get("content")
    items = content if isinstance(content, list) else []
    # tool_use 与 tool_result 的 id 关联用于 REJECT 计数
    uses = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "tool_use":
            uses[item.get("id")] = (item.get("name", ""), item.get("input") or {})
            yield item.get("name", ""), item.get("input") or {}, None, False
        elif item.get("type") == "tool_result":
            raw = item.get("content")
            if isinstance(raw, list):
                text = "\n".join(
                    x.get("text", "") for x in raw if isinstance(x, dict)
                )
            else:
                text = raw if isinstance(raw, str) else json.dumps(raw or {}, ensure_ascii=False)
            name, inp = uses.get(item.get("tool_use_id"), ("", {}))
            yield name + ":result", inp, text or "", bool(item.get("is_error"))


def analyze(files):
    m = Counter({
        "bash_total": 0, "http_calls": 0, "http_single": 0, "http_batch": 0,
        "http_sweep": 0, "http_bg": 0, "http_session": 0,
        "for_http": 0, "pyc_total": 0, "pyc_responses_parse": 0,
        "heredoc_total": 0, "heredoc_long": 0, "window_reads": 0,
        "grep_results": 0, "merge_reject": 0, "merge_rerun_max": 0,
        "bearer_literal": 0, "cookie_literal": 0,
        "evidence_calls": 0, "cred_calls": 0,
    })
    merge_command_counts = Counter()

    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    for name, inp, result_text, is_error in iter_tool_events(obj):
                        if name == "Bash":
                            cmd = inp.get("command", "") or ""
                            low = cmd.lower()
                            m["bash_total"] += 1
                            if HTTP_RE.search(cmd):
                                m["http_calls"] += 1
                                if SWEEP_RE.search(cmd):
                                    m["http_sweep"] += 1
                                elif BATCH_RE.search(cmd):
                                    m["http_batch"] += 1
                                else:
                                    m["http_single"] += 1
                                if BG_RE.search(cmd):
                                    m["http_bg"] += 1
                                if SESSION_RE.search(cmd):
                                    m["http_session"] += 1
                                if BEARER_LITERAL_RE.search(low):
                                    m["bearer_literal"] += 1
                                if COOKIE_LITERAL_RE.search(cmd):
                                    m["cookie_literal"] += 1
                                if CRED_CALL_RE.search(cmd):
                                    m["cred_calls"] += 1
                            if FOR_HTTP_RE.search(cmd):
                                m["for_http"] += 1
                            if PYC_RE.search(cmd):
                                m["pyc_total"] += 1
                                if pyc_responses_parse(cmd):
                                    m["pyc_responses_parse"] += 1
                            h_n, h_long, _ = detect_heredoc(cmd)
                            m["heredoc_total"] += h_n
                            m["heredoc_long"] += h_long
                            if WINDOW_READ_RE.search(cmd):
                                m["window_reads"] += 1
                            if EVIDENCE_CALL_RE.search(cmd):
                                m["evidence_calls"] += 1
                            dm = DELTA_FILE_RE.search(cmd)
                            if dm:
                                merge_command_counts[cmd.strip()[:200]] += 1
                        elif name == "Grep":
                            if "results" in (inp.get("path") or "").lower():
                                m["grep_results"] += 1
                        elif name.endswith(":result"):
                            # tool_use 与 tool_result 常分属不同消息，不依赖配对，
                            # 直接扫结果文本里的 REJECTED 标记。
                            if MERGE_REJECT_RE.search(result_text or ""):
                                m["merge_reject"] += 1
        except OSError:
            continue

    m["merge_rerun_max"] = max(merge_command_counts.values(), default=0)
    return m


# ── 零值哨兵：这些指标若为 0，必须人工复核检测器/样本边界 ──────────────────

ZERO_SENTINEL_METRICS = (
    "http_single", "pyc_total", "heredoc_total", "window_reads", "bash_total",
)


def main():
    parser = argparse.ArgumentParser(description="LLM 机械劳动度量（采用率验收）")
    parser.add_argument("--logs", help="Claude projects 目录（含主 jsonl 与 */subagents/）")
    parser.add_argument("--since", help="仅统计 mtime >= 该日期（YYYY-MM-DD）的日志")
    parser.add_argument("--json", action="store_true", help="机读 JSON 输出")
    parser.add_argument("--selfcheck", action="store_true", help="检测器内置正负样本断言")
    args = parser.parse_args()

    if args.selfcheck:
        run_selfcheck()
        print("[selfcheck] 全部检测器断言通过")
        return 0
    if not args.logs:
        parser.error("需要 --logs <目录>（或 --selfcheck）")

    main_files = sorted(glob.glob(os.path.join(args.logs, "*.jsonl")))
    sub_files = sorted(glob.glob(os.path.join(args.logs, "*", "subagents", "*.jsonl")))
    if args.since:
        cutoff = args.since  # 文件名无日期，用 mtime 粗滤
        import time
        cutoff_ts = time.mktime(time.strptime(cutoff, "%Y-%m-%d"))
        main_files = [f for f in main_files if os.path.getmtime(f) >= cutoff_ts]
        sub_files = [f for f in sub_files if os.path.getmtime(f) >= cutoff_ts]

    warnings = []
    if not main_files:
        warnings.append("⚠ 主会话日志 0 个：检查 --logs 路径")
    if not sub_files:
        warnings.append("⚠ 子代理日志 0 个：样本边界缺失（*/subagents/*.jsonl），结果只代表主会话")

    metrics = analyze(main_files + sub_files)
    for key in ZERO_SENTINEL_METRICS:
        if metrics.get(key, 0) == 0:
            warnings.append(f"⚠ 指标 {key}=0：被禁止行为的零值必须复核检测器与样本，禁止当作好消息")

    payload = {
        "logs_dir": os.path.abspath(args.logs),
        "sample": {"main_sessions": len(main_files), "subagent_logs": len(sub_files),
                   "since": args.since},
        "metrics": dict(metrics),
        "warnings": warnings,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[llm_labor] 样本：{len(main_files)} 主会话 + {len(sub_files)} 子代理日志")
        for key in sorted(metrics):
            print(f"  {key:24s} {metrics[key]}")
        for warning in warnings:
            print(warning)
    return 0


def run_selfcheck():
    """正负样本各手验（heredoc=0 漏报事故的制度化防御）。"""
    # heredoc 正样本（三种分隔符/引号形态）
    for cmd in (
        "python - <<'PY'\nprint(1)\nPY\n",
        "python3 - <<\"EOF\"\nx\nEOF",
        "python - <<- ENDPY\nbody\nENDPY\n",
    ):
        n, _, _ = detect_heredoc(cmd)
        assert n == 1, f"heredoc 漏检: {cmd!r}"
    # heredoc 负样本
    assert detect_heredoc("python scripts/x.py")[0] == 0
    assert detect_heredoc("python -c 'print(1)'")[0] == 0
    # 长载荷
    long_cmd = "python - <<'PY'\n" + "x = 1\n" * 80 + "PY"
    _, long_n, longest = detect_heredoc(long_cmd)
    assert long_n == 1 and longest > 400
    # -c 检测
    assert PYC_RE.search('python -c "import json"')
    assert not PYC_RE.search("python -m pytest")
    # 手写证据解析复合判据
    assert pyc_responses_parse('python -c "import json,glob; json.loads(open(f).read()) for f in glob.glob(\'responses/req_*.json\')"')
    assert not pyc_responses_parse('python -c "print(1)"')
    # for 包 http_request
    assert FOR_HTTP_RE.search("for f in a b; do python runtime/http_request.py t $f; done")
    # 明文 Bearer / Cookie
    assert BEARER_LITERAL_RE.search("-H \"Authorization: Bearer abcdef0123456789abcdef\"")
    assert COOKIE_LITERAL_RE.search("-H 'cookie: sid=abc'")
    # sweep / batch / cred
    assert SWEEP_RE.search("--sweep 'http://x/{a}'")
    assert BATCH_RE.search("--batch lines.txt")
    assert CRED_CALL_RE.search("--cred c-001")
    # REJECT 文本
    assert MERGE_REJECT_RE.search("[merge] REJECTED (evidence gate: x)")
    # 窗口化阅读
    assert WINDOW_READ_RE.search("head -c 800 req_001.txt")
    assert not WINDOW_READ_RE.search("head -n 3 file")


if __name__ == "__main__":
    sys.exit(main())
