#!/usr/bin/env python3
"""
observe.py — 纯机械观测脚本。PostToolUse hook 每次 Bash 完成后自动触发。

原则：
- 只读 memory.md（不写——防并发冲突）
- 读写自己的状态文件 .observe_state.json
- stdout 输出单行 JSON → 通过 hook 机制成为 LLM 下一轮上下文
- 始终 exit 0（hook 不阻断主流程）
- 不判断「是否正确」「是否应该换方向」——那些是 LLM 的事

修复记录：
- v2: 增量水位线读取（O(增量) 而非 O(N²)）
- v2: 命令行指纹改用 MD5 hash（消除 curl -s 正则假重复）
- v3: 指纹改用结构化提取 — Method + URL Path（MD5 在盲注/Fuzzing 场景产生假活跃）
- v3: 水位线读取增加文件截断回退防御
"""

import json
import os
import re
import sys
from urllib.parse import urlparse

MEMORY_PATH = ".claude/memory.md"
STATE_PATH = ".observe_state.json"
MAX_ACTION_HISTORY = 8

# 非推理命令：不参与 consecutive_none 递增（grep/cat/cd/echo 等不会产生认知增量）
NON_REASONING_FP_PREFIXES = {"grep", "cat", "echo", "cd", "ls", "dir", "mkdir", "empty"}

def is_non_reasoning(fp: str) -> bool:
    """检查指纹是否属于非推理命令。"""
    return fp.split(":")[0] in NON_REASONING_FP_PREFIXES


def load_stdin() -> dict:
    """从 stdin 读取 PostToolUse hook 传入的 JSON。"""
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return {}


def fingerprint(raw: str) -> str:
    """
    生成命令的「结构性指纹」——识别同一种战术的重复，而非同一条命令的重复。

    v3: 对于 curl，提取 Method + URL Path（剥离 query 参数和 fragment）。
        无论 LLM 换多少 payload，只要还在同一个接口上 → 指纹相同。
        配合 prefix 停滞检测区分「有效盲注」和「无产出死磕」。

    对于其他命令，取命令名前缀。
    """
    if not raw:
        return "empty"

    cmd = raw.strip()

    # ── http_request 专项：提取 Method + URL Path ──
    # agent 通过 python runtime/http_request.py <target> <curl_args> 发送请求
    # 支持相对路径 (runtime/...) 和绝对路径 (F:/@@sec_tools/.../runtime/...)
    http_req_match = re.search(r'python3?\s+["\']?[^\s"\']*http_request\.py["\']?\s+', cmd)
    if http_req_match:
        method = "GET"
        x_match = re.search(r'-X\s+(\S+)', cmd, re.IGNORECASE)
        if x_match:
            method = x_match.group(1).upper()
        url_match = re.search(r"""['"]?(https?://[^\s'"<>]+)['"]?""", cmd)
        if url_match:
            url = url_match.group(1)
            try:
                parsed = urlparse(url)
                path = parsed.path.rstrip('/') or '/'
                return f"HTTP:{method}:{parsed.hostname}{path}"
            except Exception:
                host_match = re.search(r'https?://([^/\s"\'<>]+)', cmd)
                if host_match:
                    return f"HTTP:{method}:{host_match.group(1)}"
        return "HTTP:unknown"

    # ── curl 专项：提取 Method + URL Path ──
    curl_match = re.search(r'curl\s+', cmd)
    if curl_match:
        # 提取 HTTP Method（-X 参数）
        method = "GET"
        x_match = re.search(r'-X\s+(\S+)', cmd, re.IGNORECASE)
        if x_match:
            method = x_match.group(1).upper()

        # 提取 URL（支持单引号、双引号、裸 URL）
        url_match = re.search(r"""['"]?(https?://[^\s'"<>]+)['"]?""", cmd)
        if url_match:
            url = url_match.group(1)
            try:
                parsed = urlparse(url)
                path = parsed.path.rstrip('/') or '/'
                return f"{method}:{parsed.hostname}{path}"
            except Exception:
                # URL 解析失败 → 回退到 hostname 级指纹
                host_match = re.search(r'https?://([^/\s"\'<>]+)', cmd)
                if host_match:
                    return f"{method}:{host_match.group(1)}"

        # 有 curl 但没提取到 URL → 用第一个非 option 参数
        words = cmd.split()
        for w in words[1:]:
            if not w.startswith('-'):
                return f"curl:{w[:60]}"
        return "curl:unknown"

    # ── 其他命令 ──
    cmd_patterns = [
        (r"(nmap\s+\S+)", "nmap"),
        (r"(sqlmap\s+\S+)", "sqlmap"),
        (r"(gobuster\s+\S+)", "gobuster"),
        (r"(hydra\s+\S+)", "hydra"),
        (r"(john\s+\S+)", "john"),
        (r"(python3?\s+(\S+\.py))", "python"),
        (r"(grep\s+\S+)", "grep"),
        (r"(cat\s+\S+)", "cat"),
        (r"(echo\s+\S+)", "echo"),
        (r"(openssl\s+\S+)", "openssl"),
    ]
    for pattern, label in cmd_patterns:
        match = re.search(pattern, cmd)
        if match:
            extracted = match.group(1)
            if len(extracted) > 60:
                extracted = extracted[:57] + "..."
            return f"{label}:{extracted}"

    # ── 本地二进制：提取可执行文件名（剥离可变参数） ──
    # ./vuln_bin payload_1 → vuln_bin
    # /usr/bin/exploit arg1 arg2 → exploit
    first_word = cmd.split()[0] if cmd.split() else "unknown"
    if first_word.startswith("./") or first_word.startswith("/"):
        import os as _os
        return f"bin:{_os.path.basename(first_word)}"

    # 兜底
    return f"{first_word}:{cmd[:40]}"


def describe_action(raw: str) -> str:
    """生成供 LLM 阅读的动作描述（≤80 字符，旁路，不参与指纹比对）。"""
    if not raw:
        return "empty"
    cmd = raw.strip()
    if len(cmd) <= 80:
        return cmd
    return cmd[:77] + "..."


def load_state() -> dict:
    """加载观测状态文件。"""
    if not os.path.exists(STATE_PATH):
        return {
            "actions": [],
            "prefix_counts": {},
            "memory_offset": 0,
            "last_round": 0,
        }
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {"actions": [], "prefix_counts": {}, "memory_offset": 0, "last_round": 0}


def save_state(state: dict):
    """写回状态文件（独立于 memory.md，避免并发冲突）。"""
    state["version"] = 3
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def count_new_prefixes(new_content: str) -> dict:
    """统计新增内容中各前缀的数量。O(增量) 而非 O(全量)。"""
    if not new_content:
        return {}
    lines = new_content.split("\n")
    counts = {}
    for prefix in ["vuln:", "hypothesis:", "surface:", "cred:"]:
        key = prefix.rstrip(":")
        counts[key] = sum(1 for line in lines if line.strip().startswith(prefix))
    return counts


def read_memory_incremental(state: dict) -> dict:
    """
    水位线增量读取 memory.md。
    v3: 增加文件截断防御 —— 如果文件被外部重置（size < offset），从头重读。
    """
    if not os.path.exists(MEMORY_PATH):
        return state.get("prefix_counts", {})

    offset = state.get("memory_offset", 0)

    # ── 文件截断防御 ──
    try:
        current_size = os.path.getsize(MEMORY_PATH)
    except OSError:
        current_size = 0

    if offset > current_size:
        # 文件被外部重置（truncate/重写）→ 重置水位线，从头全量读
        offset = 0
        state["memory_offset"] = 0

    with open(MEMORY_PATH, "r", encoding="utf-8") as f:
        f.seek(offset)
        new_content = f.read()
        new_offset = f.tell()

    if not new_content:
        return state.get("prefix_counts", {})

    new_counts = count_new_prefixes(new_content)
    current = dict(state.get("prefix_counts", {}))
    for k, v in new_counts.items():
        current[k] = current.get(k, 0) + v

    # 更新总行数/总字符数
    try:
        total_size = os.path.getsize(MEMORY_PATH)
        with open(MEMORY_PATH, "r", encoding="utf-8") as f:
            total_lines = sum(1 for _ in f)
        current["total_lines"] = total_lines
        current["total_chars"] = total_size
    except OSError:
        current["total_lines"] = current.get("total_lines", 0)
        current["total_chars"] = current.get("total_chars", 0)

    state["memory_offset"] = new_offset
    return current


def read_memory_full() -> dict:
    """全量读取（仅首次运行，用于初始化水位线）。"""
    if not os.path.exists(MEMORY_PATH):
        return {"total_lines": 0, "total_chars": 0}

    with open(MEMORY_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    counts = {}
    lines = content.split("\n")
    for prefix in ["vuln:", "hypothesis:", "surface:", "cred:"]:
        key = prefix.rstrip(":")
        counts[key] = sum(1 for line in lines if line.strip().startswith(prefix))
    counts["total_lines"] = len(lines)
    counts["total_chars"] = len(content)
    return counts


def build_output(
    action_fp: str,
    action_desc: str,
    state: dict,
    current_counts: dict,
) -> dict:
    """构建观测输出。只产出中性物理量。"""

    # ── 动作重复度 ──
    actions = state["actions"]
    same_action_count = 0
    for a in reversed(actions):
        if a["fp"] == action_fp:
            same_action_count += 1
        else:
            break

    # ── 停滞检测（分层） ──
    prev = state.get("prefix_counts", {})
    new_vuln = current_counts.get("vuln", 0) - prev.get("vuln", 0)
    new_surface_or_hypothesis = (
        current_counts.get("surface", 0) - prev.get("surface", 0)
        + current_counts.get("hypothesis", 0) - prev.get("hypothesis", 0)
    )
    new_cred = current_counts.get("cred", 0) - prev.get("cred", 0)
    total_new = new_vuln + new_surface_or_hypothesis + new_cred

    consecutive_none = state.get("consecutive_none", 0)
    if total_new <= 0:
        consecutive_none += 1
    else:
        consecutive_none = 0

    gc_warn = None
    if current_counts.get("surface", 0) > 100:
        gc_warn = "surface>100"
    elif current_counts.get("hypothesis", 0) > 30:
        gc_warn = "hypothesis>30"

    return {
        "round": state.get("last_round", 0) + 1,
        "same_action_count": same_action_count,
        "action": action_desc[:80],
        "new_vuln": new_vuln,
        "new_surface_hypothesis": new_surface_or_hypothesis,
        "new_cred": new_cred,
        "consecutive_none_rounds": consecutive_none,
        "total_vuln": current_counts.get("vuln", 0),
        "memory_size": f'{current_counts.get("total_lines", 0)}L/{current_counts.get("total_chars", 0)}c',
        "gc_alert": gc_warn,
    }


def main():
    hook_input = load_stdin()
    tool_input = hook_input.get("tool_input", {})
    raw_command = tool_input.get("command", "") if isinstance(tool_input, dict) else str(tool_input)

    action_fp = fingerprint(raw_command)
    action_desc = describe_action(raw_command)
    state = load_state()

    # 水位线增量读取（首次全量）
    is_first_run = state.get("memory_offset", 0) == 0 and not state.get("prefix_counts")
    if is_first_run:
        current_counts = read_memory_full()
        if os.path.exists(MEMORY_PATH):
            state["memory_offset"] = os.path.getsize(MEMORY_PATH)
    else:
        current_counts = read_memory_incremental(state)

    state["last_round"] = state.get("last_round", 0) + 1
    state["prefix_counts"] = current_counts

    state["actions"].append({"fp": action_fp, "round": state["last_round"]})
    if len(state["actions"]) > MAX_ACTION_HISTORY:
        state["actions"] = state["actions"][-MAX_ACTION_HISTORY:]

    output = build_output(action_fp, action_desc, state, current_counts)
    # 非推理命令（grep/cat/cd/echo）：仍检测 memory 增长（growth 时 reset consecutive_none），
    # 但本轮不递增 consecutive_none（这些命令本身不会产出认知增量）。
    # 只在「无增长且需要 undo 递增」时才覆盖，有增长时保持 build_output 已 reset 的 0。
    if is_non_reasoning(action_fp) and output["consecutive_none_rounds"] > 0:
        output["consecutive_none_rounds"] = state.get("consecutive_none", 0)
    state["consecutive_none"] = output["consecutive_none_rounds"]
    state["same_action_count"] = output["same_action_count"]
    state["gc_alert"] = output["gc_alert"]

    save_state(state)
    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 兜底：任何错误都输出有效 JSON + exit 0，不阻断主流程
        print(json.dumps({
            "round": 0, "same_action_count": 0, "action": "error",
            "new_vuln": 0, "new_surface_hypothesis": 0,
            "new_cred": 0, "consecutive_none_rounds": 0,
            "total_vuln": 0,
            "memory_size": "0L/0c", "gc_alert": None,
            "observe_error": str(e)[:200]
        }, ensure_ascii=False))
        sys.exit(0)
