#!/usr/bin/env python3
"""阻止工具直接写入热 memory；Runtime merge.py 仍可机械更新。

覆盖两类写入路径：
1. Write/Edit 工具直接指向 memory.md
2. Bash 命令通过重定向（> 或 >>）写入 memory.md
"""

import json
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_PATH = os.path.normcase(os.path.join(PROJECT_ROOT, ".claude", "memory.md"))

# 匹配 Bash 中 > 或 >> 重定向到 memory.md 的模式
_BASH_REDIRECT_RE = re.compile(
    r""">{1,2}\s*['"]?.*?\.claude[/\\]memory\.md['"]?""",
    re.IGNORECASE,
)


def _is_memory_write(tool_name: str, tool_input: dict) -> bool:
    """检测是否是对热 memory 的直接写入。"""
    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        if file_path:
            candidate = os.path.normcase(os.path.abspath(file_path))
            return candidate == MEMORY_PATH

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        # merge.py 自身的写入是允许的
        if "merge.py" in command:
            return False
        if _BASH_REDIRECT_RE.search(command):
            return True

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

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "热 memory 只接受 runtime/merge.py 的 DELTA 更新。"
                                        "请执行：echo \"DELTA:\\n<lines>\" | python runtime/merge.py",
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
