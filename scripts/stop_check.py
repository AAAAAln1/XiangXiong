#!/usr/bin/env python3
"""
stop_check.py — Stop hook 自检脚本。LLM 每轮回复结束时自动触发。

两类职责（冻结分离）：
1) contract enforcement — decision:block（与 turn 计数无关）
2) passive sensors
   - target loop: stagnation_level（来自 .observe_state / consecutive_none）
   - turn telemetry: turn_tool_use_count / turns_without_tool_use（.turn_state.json）
     Turn event telemetry ONLY. Not idle detection. MUST NOT affect enforcement.
     Does not fix Host generation timeout.

原则：
- 绝不用 exit 2（不强制 LLM 继续工作）
- 传感器不替大脑做决定（无 MUST/STOP/SWITCH 动词于 stagnation 输出）
- turn 字段仅在 turn_tool_use_count==0 时输出两个数字；无 level/idle/watch/stuck
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
TURN_STATE_PATH = os.path.join(_PROJECT_ROOT, ".turn_state.json")
TURN_STATE_VERSION = 1
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


def load_turn_state() -> dict:
    """Turn lifecycle only — not target observation."""
    if not os.path.exists(TURN_STATE_PATH):
        return {"version": TURN_STATE_VERSION, "turns_without_tool_use": 0}
    try:
        with open(TURN_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"version": TURN_STATE_VERSION, "turns_without_tool_use": 0}
        return {
            "version": TURN_STATE_VERSION,
            "turns_without_tool_use": int(data.get("turns_without_tool_use", 0) or 0),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"version": TURN_STATE_VERSION, "turns_without_tool_use": 0}


def save_turn_state(turns_without_tool_use: int) -> None:
    payload = {
        "version": TURN_STATE_VERSION,
        "turns_without_tool_use": max(0, int(turns_without_tool_use)),
    }
    try:
        with open(TURN_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError:
        pass


def reset_turn_state() -> None:
    """换 target 时机械清零（merge 调用）。"""
    save_turn_state(0)


def is_human_user_entry(entry: dict) -> bool:
    """Human turn boundary — not tool_result-only user rows."""
    if entry.get("type") != "user":
        return False
    message = entry.get("message", {})
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        types = [b.get("type") for b in content if isinstance(b, dict)]
        if not types:
            return False
        if all(t == "tool_result" for t in types):
            return False
        return any(t == "text" for t in types) or any(
            t not in ("tool_result",) for t in types
        )
    return False


def count_tool_use_in_content(content) -> int:
    if not isinstance(content, list):
        return 0
    n = 0
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            n += 1
    return n


def count_turn_tool_uses(transcript_path: str) -> int:
    """本轮（自上一 human user 起）assistant 消息中的 tool_use 条数。"""
    if not transcript_path or not os.path.exists(transcript_path):
        return 0
    count = 0
    try:
        with open(transcript_path, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if is_human_user_entry(entry):
                    count = 0
                    continue
                if entry.get("type") != "assistant":
                    continue
                message = entry.get("message", {})
                if not isinstance(message, dict):
                    continue
                count += count_tool_use_in_content(message.get("content"))
    except OSError:
        return 0
    return count


def update_turn_telemetry(transcript_path: str) -> tuple:
    """
    Stop 独占：根据本轮 tool_use 更新 turns_without_tool_use。
    返回 (turn_tool_use_count, turns_without_tool_use)。
    不写 world；不影响 decision:block。
    """
    turn_tool_use_count = count_turn_tool_uses(transcript_path)
    state = load_turn_state()
    prev = state.get("turns_without_tool_use", 0)
    if turn_tool_use_count == 0:
        turns_without_tool_use = prev + 1
    else:
        turns_without_tool_use = 0
    save_turn_state(turns_without_tool_use)
    return turn_tool_use_count, turns_without_tool_use


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

    transcript_path = hook_input.get("transcript_path", "") or ""

    # --- (1) contract enforcement：与 turn telemetry 无关；先于传感器 ---
    last_message = load_last_assistant_message(transcript_path)
    conflict_reason = stop_conflict_reason(last_message)
    if conflict_reason:
        print(json.dumps({
            "decision": "block",
            "reason": conflict_reason,
        }, ensure_ascii=False))
        sys.exit(0)

    # --- (2a) turn event telemetry：独占 .turn_state.json；永不 decision:block ---
    turn_tool_use_count, turns_without_tool_use = update_turn_telemetry(transcript_path)
    turn_payload = None
    if turn_tool_use_count == 0:
        turn_payload = {
            "turn_tool_use_count": 0,
            "turns_without_tool_use": turns_without_tool_use,
        }

    # --- (2b) target-loop stagnation：.observe_state only；语义不变 ---
    state = load_state()
    stag_payload = None
    if state is not None:
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
        if level != "normal":
            # 仅在 warning+ 时输出。纯度量标签，零动作动词。
            stag_payload = {
                "stagnation_level": level,
                "metrics": {
                    "consecutive_none_rounds": consecutive_none,
                    "same_action_count": same_count,
                    "total_vuln": state.get("prefix_counts", {}).get("vuln", 0),
                    "total_breakthrough": state.get("prefix_counts", {}).get("breakthrough", 0),
                },
            }

    # 分开发布：不把 turn 折进 stagnation，不把 level 加进 turn
    if turn_payload is not None:
        print(json.dumps(turn_payload, ensure_ascii=False))
    if stag_payload is not None:
        print(json.dumps(stag_payload, ensure_ascii=False))

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
