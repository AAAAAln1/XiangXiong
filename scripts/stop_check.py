#!/usr/bin/env python3
"""
stop_check.py — Stop hook 自检脚本。LLM 每轮回复结束时自动触发。

原则：
- 绝不用 exit 2（不强制 LLM 继续工作）
- 提供计算后的停滞等级（stagnation_level）——类似油表显示"E"而非只说"2升"
- 不含任何动作动词（MUST/STOP/SWITCH）——传感器不替大脑做决定
- LLM 的 Observer 六问 + Synthesizer 负责"该怎么办"

v3: stagnation_level 替代纯数字——既保持传感器边界，又提高注意力捕获。
"""

import json
import os
import re
import sys

# 强制 stdin/stdout/stderr 使用 UTF-8（Windows GBK 环境必须显式设置）
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 使用绝对路径——Stop hook cwd 不确定
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
STATE_PATH = os.path.join(_PROJECT_ROOT, ".observe_state.json")
CONTINUE_QUESTION_PATTERNS = (
    re.compile(r"需要我.{0,30}继续.{0,8}[吗？?]"),
    re.compile(r"是否.{0,20}继续.{0,8}[？?]"),
    re.compile(r"要不要.{0,20}(继续|深入)"),
)
PENDING_WORK_PATTERNS = (
    re.compile(r"^(?:#+\s*)?(?:下一步|后续)(?:方向|计划|验证|测试)?\s*[:：]?\s*$", re.MULTILINE),
    re.compile(r"下一步.{0,40}(?:验证|测试|探测|尝试|寻找)"),
)

# 停滞等级阈值（仅用于计算标签，不做决策）
# - normal:    一切正常
# - warning:   连续无产出 5-8 轮，或同动作连续 4-6 次
# - critical:  连续无产出 ≥9 轮，或同动作连续 ≥7 次
# - emergency: 连续无产出 ≥15 轮，且同动作 ≥10 次


def load_state():
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        version = data.get("version", 0)
        if version != 3:
            raise ValueError(f".observe_state.json version={version}, expected 3. "
                             f"File format changed — check observe.py for schema updates.")
        return data
    except (json.JSONDecodeError, FileNotFoundError):
        return None


def load_last_assistant_message(transcript_path: str) -> str:
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    last_message = ""
    try:
        with open(transcript_path, "r", encoding="utf-8") as file:
            for line in file:
                entry = json.loads(line)
                message = entry.get("message", {})
                if entry.get("type") != "assistant" or not isinstance(message, dict):
                    continue
                content = message.get("content", "")
                if isinstance(content, str):
                    last_message = content
                elif isinstance(content, list):
                    last_message = "".join(
                        item.get("text", "") for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    )
    except (OSError, json.JSONDecodeError):
        return ""
    return last_message


def asks_user_to_continue(text: str) -> bool:
    return any(pattern.search(text) for pattern in CONTINUE_QUESTION_PATTERNS)


def declares_pending_work(text: str) -> bool:
    return any(pattern.search(text) for pattern in PENDING_WORK_PATTERNS)


_MEMORY_PREFIX_RE = re.compile(
    r"(?m)^(?:target|tech|port|artifact|surface|vuln|cred|hypothesis|counter|dead|future):[^\n]+$"
)
_DELTA_KEYWORD_RE = re.compile(r"(?:DELTA|delta)[：:\s]")

def has_unapplied_delta(text: str) -> bool:
    # 方式一：包含独立 DELTA: 标题行
    if re.search(r"(?m)^\s*DELTA:\s*$", text):
        return True
    # 方式二：Agent 声明"输出 DELTA"且文本中有合法前缀行
    # （Agent 把 DELTA 内容写进聊天但没有执行管道命令）
    if _DELTA_KEYWORD_RE.search(text) and len(_MEMORY_PREFIX_RE.findall(text)) >= 2:
        return True
    return False


def stop_conflict_reason(text: str) -> str:
    if asks_user_to_continue(text):
        return "最终回答正在询问用户是否继续。按当前 Mission 状态自行继续或自然结束，不把该决定交还用户。"
    if declares_pending_work(text):
        return "最终回答已声明下一步仍有验证工作。继续执行已声明的下一实验；只有不再存在可验证方向时才自然结束。"
    if has_unapplied_delta(text):
        return "最终回答包含尚未应用的 DELTA。先通过 runtime/merge.py 持久化该认知增量，再继续或结束。"
    return ""


def compute_level(consecutive_none: int, same_count: int) -> str:
    """纯计算：把物理量映射到标签等级。标签只是读数的另一种表示。"""
    if consecutive_none >= 15 and same_count >= 10:
        return "emergency"
    if consecutive_none >= 9 or same_count >= 7:
        return "critical"
    if consecutive_none >= 5 or same_count >= 4:
        return "warning"
    return "normal"


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}
    last_message = load_last_assistant_message(hook_input.get("transcript_path", ""))
    conflict_reason = stop_conflict_reason(last_message)
    if conflict_reason:
        print(json.dumps({
            "decision": "block",
            "reason": conflict_reason,
        }, ensure_ascii=False))
        sys.exit(0)

    state = load_state()
    if state is None:
        sys.exit(0)

    consecutive_none = state.get("consecutive_none", 0)
    same_action = state.get("actions", [])
    same_count = 0
    if same_action:
        last_fp = same_action[-1]["fp"]
        for a in reversed(same_action):
            if a["fp"] == last_fp:
                same_count += 1
            else:
                break

    level = compute_level(consecutive_none, same_count)

    if level == "normal":
        sys.exit(0)

    # 仅在 warning+ 时输出。纯度量标签，零动作动词。
    print(json.dumps({
        "stagnation_level": level,
        "metrics": {
            "consecutive_none_rounds": consecutive_none,
            "same_action_count": same_count,
            "total_vuln": state.get("prefix_counts", {}).get("vuln", 0),
            "total_breakthrough": state.get("prefix_counts", {}).get("breakthrough", 0),
        }
    }, ensure_ascii=False))

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
