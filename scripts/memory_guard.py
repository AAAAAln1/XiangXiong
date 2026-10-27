#!/usr/bin/env python3
"""阻止工具绕过 Runtime 直接修改世界状态文件；merge/experiment 仍是唯一 mutation 入口。

受保护路径集（World Model Mutation Contract）：
1. active memory：<root>/.claude/memory.md
2. 归档 memory：<root>/results/<world>/memory.md
3. artifact 身份源：<root>/results/<world>/artifacts/index.json（可伪造证据存在性 → 架空 Evidence Gate）
4. runtime 状态：<root>/runtime/runtime.json、<root>/.turn_state.json

拦截策略：路径段邻接匹配，与命令语义无关——
- Write/Edit：file_path 命中保护集 → deny
- Bash：命令中出现保护路径段（如 `.claude\\memory.md`、`results\\x\\memory.md`、
  `artifacts\\index.json`）且不在 runtime 工具白名单 → deny
- Bash 白名单：merge.py / experiment.py / execute.py / session_start.py / validate_persistence.py

误伤边界（v2 修复）：路径段必须邻接。`C:/Users/x/.claude/projects/F-.../memory/MEMORY.md`
（宿主 auto-memory）不含 `.claude/memory.md` 邻接段 → 放行，不再因 token 共存误拦。
不做命令语义分析，不判断意图。读文件用 Read 工具（不在 matcher 内）。
"""

import json
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "runtime"))
from hook_patterns import RUNTIME_CALL_RE as _RUNTIME_CALL_RE  # noqa: E402
from hook_patterns import SHELL_TOOLS as _SHELL_TOOLS  # noqa: E402
from hook_patterns import split_segments as _split_segments  # noqa: E402

ACTIVE_MEMORY_PATH = os.path.normcase(os.path.join(PROJECT_ROOT, ".claude", "memory.md"))
RESULTS_DIR = os.path.normcase(os.path.join(PROJECT_ROOT, "results"))
RUNTIME_JSON_PATH = os.path.normcase(os.path.join(PROJECT_ROOT, "runtime", "runtime.json"))
TURN_STATE_PATH = os.path.normcase(os.path.join(PROJECT_ROOT, ".turn_state.json"))

# 路径段邻接模式（/ 与 \ 两种分隔符；IGNORECASE 覆盖盘符大小写）
# 2026-10-06 P0-d 新增三类内网改造保护文件：
#   scope.json（授权声明，防自造授权架空 scope 闸）、
#   credentials/index.json 与 credentials/c-NNN 值文件（凭据编号体系只能经 credentials.py 写）
_BASH_PROTECTED_RES = (
    re.compile(r"\.claude[/\\]memory\.md", re.IGNORECASE),
    re.compile(r"results[/\\][^/\\]+[/\\]memory\.md", re.IGNORECASE),
    re.compile(r"results[/\\][^/\\]+[/\\]artifacts[/\\]index\.json", re.IGNORECASE),
    re.compile(r"results[/\\][^/\\]+[/\\]scope\.json", re.IGNORECASE),
    re.compile(r"results[/\\][^/\\]+[/\\]credentials[/\\]index\.json", re.IGNORECASE),
    re.compile(r"results[/\\][^/\\]+[/\\]credentials[/\\]c-\d+\.txt", re.IGNORECASE),
    re.compile(r"runtime[/\\]runtime\.json", re.IGNORECASE),
    re.compile(r"(?:^|[/\\\s])\.turn_state\.json", re.IGNORECASE),
)

# 窄域补充：重定向目标恰为 memory.md 且命令含项目目录 token（cd .claude && echo > memory.md 这类相对路径写法）
_BASH_BARE_REDIRECT_RE = re.compile(r">{1,2}\s*[\"']?memory\.md", re.IGNORECASE)


def _is_protected_path(path: str) -> bool:
    """Write/Edit 的 file_path 是否指向受保护文件（normcase 精确/前缀匹配）。"""
    candidate = os.path.normcase(os.path.abspath(path))
    if candidate in (ACTIVE_MEMORY_PATH, RUNTIME_JSON_PATH, TURN_STATE_PATH):
        return True
    parent = os.path.dirname(candidate)
    if parent.startswith(RESULTS_DIR + os.sep) and os.path.basename(candidate) == "memory.md":
        return True
    # 仅当路径位于 results/<world>/ 下时才判内网改造保护文件
    if parent.startswith(RESULTS_DIR + os.sep):
        rel = os.path.relpath(candidate, RESULTS_DIR)
        parts = rel.split(os.sep)
        if len(parts) >= 2:
            rest = parts[1:]
            # artifact 身份源：results/<world>/artifacts/index.json
            if rest == ["artifacts", "index.json"]:
                return True
            # 授权声明：results/<world>/scope.json（只能经 scope.py init/position 写）
            if rest == ["scope.json"]:
                return True
            # 凭据索引与值文件：只能经 credentials.py 写
            if rest == ["credentials", "index.json"]:
                return True
            if len(rest) == 2 and rest[0] == "credentials" and re.fullmatch(r"c-\d+\.txt", rest[1]):
                return True
    return False


def _bash_touches_protected(command: str) -> bool:
    """Bash 命令字符串中是否出现受保护路径段（机械匹配，不解析语义）。"""
    if any(pattern.search(command) for pattern in _BASH_PROTECTED_RES):
        return True
    if _BASH_BARE_REDIRECT_RE.search(command):
        lowered = command.lower()
        if ".claude" in lowered or "results" in lowered:
            return True
    return False


def _is_memory_write(tool_name: str, tool_input: dict) -> bool:
    """检测是否是对受保护世界状态文件的绕道写入。"""
    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        if not file_path:
            return False
        return _is_protected_path(file_path)

    if tool_name in _SHELL_TOOLS:
        command = tool_input.get("command", "")
        if not command:
            return False
        # 按命令链切段：runtime 工具段豁免，其余段仍按保护路径判。
        # 豁免段的上下文（如 `.claude` 出现在别段）只用于裸重定向判定，不整条放行。
        segments = [s for s in _split_segments(command) if s.strip()]
        runtime_segments = [s for s in segments if _RUNTIME_CALL_RE.search(s)]
        if runtime_segments:
            others = " ".join(s for s in segments if not _RUNTIME_CALL_RE.search(s))
            if any(pattern.search(others) for pattern in _BASH_PROTECTED_RES):
                return True
            if _BASH_BARE_REDIRECT_RE.search(others):
                lowered = command.lower()
                if ".claude" in lowered or "results" in lowered:
                    return True
            return False
        return _bash_touches_protected(command)

    return False


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return

    if not _is_memory_write(tool_name, tool_input):
        return

    # 文案必须是**当前被证实可用**的那条路。
    # 历史 bug（2026-09-18）：这里曾教 `echo "DELTA:\n<lines>" | python runtime/merge.py` ——
    # 而那条命令在 Git Bash / PowerShell 下**静默记 0 条**（echo 不解释 \n → 整段挤成一行
    # → parse_delta 析出 0 个操作 → exit 0）。于是本 hook 恰好在 agent 最困惑、最需要
    # 兜底的时刻，把它推向一个"看起来成功、实际什么都没记"的动作。见 tests/test_cli_ergonomics.py。
    reason = ("世界状态/授权/凭据文件（memory.md / artifacts/index.json / scope.json / "
              "credentials/index.json 与 c-NNN 值文件 / runtime.json / .turn_state.json）"
              "禁止直接**写**。"
              "读：主 agent 用 Read 工具（不经本守卫）；探索者按 deepdive.md 只读邻域，"
              "不读 memory 全文。看现状用 python runtime/project.py。"
              "写：先把 DELTA 用 Write 落盘"
              "（results/<target>/scripts/_delta.txt），再跑 "
              "python runtime/merge.py --delta-file <该路径>（零 shell 转义）；"
              "实验结论走：python runtime/experiment.py close --memory-update-file <path>")

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
