#!/usr/bin/env python3
"""PreToolUse hook：禁止 agent 向用户发问（AskUserQuestion）。

设计（2026-09-25 与用户确认，方案 B+C）：
- 用户要求 agent 完全按已知信息测完直接交 report，不发问。
- 提示层（CLAUDE.md「禁止问人」）对弱模型不可靠（有 opus-4.8 绕过 experiment 的前科），
  故用与 memory_guard 同款的机械 deny 兜底。
- deny 的理由**必须是替代动作**（本仓库哲学：把下一步降到"抄一行"），所以文案直接给
  三类等价路径：缺条件→[blocked]、scope 争议→默认不测+边界、其余→绕过/跳过。
- 拦不住"文本请示"（模型直接在回复里问）→ 由 CLAUDE.md 提示层负责，见第 3 行。
"""
import json
import sys

REASON = (
    "禁止问人（CLAUDE.md：按已知信息测完直接交 report，AskUserQuestion 与文本请示都不许）。"
    "替代动作：①缺条件（无账号/凭据/环境）→ 记 hypothesis [blocked] 写明缺什么，"
    "继续把无条件面测完；②scope 争议（关联域/第三方资产）→ 默认不测，记 counter:/dead: 边界；"
    "③资源缺失（本地环境/接收器等）→ 跳过或换等价路径。"
    "要问的内容一律改写进 report.md 的边界清单，交报告时一并给出。"
)


def main() -> None:
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON,
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()