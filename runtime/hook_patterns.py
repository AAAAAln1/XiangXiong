#!/usr/bin/env python3
"""hook_patterns.py — PreToolUse 守卫共享的机械匹配（单一实现）。

memory_guard 与 inline_guard 都要做两件相同的事：
1. 识别「runtime 工具调用段」（命令链中该段享受豁免）；
2. 按命令链切段（&& / || / ; / | / 换行），豁免只作用于 runtime 所在段。

历史上这些正则只存在于 scripts/memory_guard.py；inline_guard 再抄一份就是
第五次副本漂移（前四：EVIDENCE_BLOCK_RE/normalize_status/filelock/digest）。
故下沉到 runtime/，两个 guard 都从这里取。scripts/ 非包结构，sys.path 含 runtime/
（hook 由 `python scripts/x.py` 启动，文件内自行 insert 项目 runtime 目录）。
"""

from __future__ import annotations

import re

# ── shell 工具名集合（单一实现，2026-10-11）─────────────────────────
# 本机存在第二条 shell 通路 PowerShell（工具名首字母大写）。它原先既不在
# settings 的 hook matcher 里，也不在 permissions.allow 里（那里写的是
# 小写 s 的 `Powershell(*)`，与真实工具名大小写不符），因此绕开全部 hook。
#
# 真实使用频次：全量会话普查 123 文件 / Bash 6553 次 / PowerShell 8 次，
# 且 8 次全部来自探针自身 → **latent 缺口，不是 observed**。
# 但 Stage 1 大量 spawn headless session，模型选shell 通路的行为可能改变，
# 所以按"通路存在即看守"处理。
#
# 放在这里而不是各自硬编码：本文档的 docstring 已经记录了五处副本漂移的学费，
# 不该造第六处。以后加 Zsh / fish / cmd 只改这一行。
SHELL_TOOLS = frozenset({"Bash", "PowerShell", "Powershell"})

# Bash 白名单：仅放行"真实 runtime 工具调用形态"，拒绝仅字符串包含工具名的命令。
# 形态：python/python3 ... runtime/<tool>.py
RUNTIME_CALL_RE = re.compile(
    r"python[0-9.]*[^&|]*runtime[/\\](?:merge|experiment|execute|session_start|validate_persistence"
    r"|scope|credentials|intranet_scan|evidence|http_request)\.py",
    re.IGNORECASE,
)

# 命令链切分：白名单**按段豁免**。引号内分隔符会被误切（保守方向：误切只多判，不会少判）。
CMD_SEG_RE = re.compile(r"\s*(?:&&|\|\||[;|\n])\s*")


def split_segments(command: str) -> list:
    """把命令链切成段（含空白段已保留，由调用方过滤）。"""
    return CMD_SEG_RE.split(command)
