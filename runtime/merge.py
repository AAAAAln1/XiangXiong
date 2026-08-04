#!/usr/bin/env python3
"""
merge.py — 读取 LLM 输出的 DELTA，对 memory.md 执行纯 CRUD。

原则：
- LLM 不直接写 memory.md。只输出 DELTA。
- merge.py 只做机械操作：ADD / UPDATE / DELETE，以及 target 边界隔离。
- 禁止任何"智能"逻辑——不判断根域、不继承 hypothesis、不推理漏洞。
- Runtime 不长脑子。merge.py 是纯机械操作。

World boundary（机械）：
- Active memory 有且只能有一个 active target（一个 world）。
- DELTA 写入新 target: 且与现有 active 不一致时：
  archive(old) → clear(active) → init(new) → 再应用本批其余 ops。
- 不判断公司/根域/是否该继承；跨 target 知识只在 results/<target>/memory.md。

用法：
  echo "DELTA:
  surface: /api/login 存在认证机制
  hypothesis: 认证可被绕过 [0.6]" | python runtime/merge.py
"""

import json
import os
import re
import sys

# 强制 stdin/stdout/stderr 使用 UTF-8，绕过 Windows GBK 系统编码
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(_PROJECT_ROOT, "results")
OBSERVE_STATE_PATH = os.path.join(_PROJECT_ROOT, ".observe_state.json")
TURN_STATE_PATH = os.path.join(_PROJECT_ROOT, ".turn_state.json")

try:
    from .paths import world_active
except ImportError:
    from paths import world_active

# 合法前缀白名单。ADD 操作须以此列表中的任一前缀开头，否则拒绝写入。
# 注意：此列表与 ARCHITECTURE.md §6 + CLAUDE.md 保持同步。
# 认知对象（6 个）：surface, hypothesis, counter, vuln, cred, dead
# 目标档案（4 个）：target, tech, port, artifact
# 特殊标签：future（战略传承，不参与推理）
# 状态标记（hypothesis 可选）：[observed] [confirmed] [contradicted] [blocked] [unknown]
#   例如：hypothesis [observed]: check_token 可能被限制访问
VALID_PREFIXES = [
    "target:", "tech:", "port:", "artifact:",
    "surface:", "vuln:", "cred:",
    "hypothesis:", "hypothesis [", "counter:", "dead:",
    "future:",
]


def _is_valid_line(line: str) -> bool:
    """检查行是否以合法前缀开头且含内容（严格单行 prefix: content 格式）。

    事务边界防御：拒绝「裸 prefix 行」（如 ``vuln:`` 后无内容）。
    半截 DELTA（session 中断在块结构中段）的典型特征就是裸 prefix 行。
    """
    stripped = line.strip()
    if not stripped:
        return False
    for p in VALID_PREFIXES:
        if stripped.startswith(p):
            # 必须有内容：prefix 之后至少 1 个非空字符
            content = stripped[len(p):].strip()
            if not content:
                print(f"[merge] REJECTED (naked prefix, no content): {stripped[:60]}", file=sys.stderr)
                return False
            return True
    return False


def read_memory() -> list:
    """读取 active world memory 全部行（路径经 paths.world_active）。"""
    path = world_active()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return f.readlines()


def write_memory(lines: list):
    """写回 active world memory（路径经 paths.world_active）。"""
    path = world_active()
    content = "".join(lines).rstrip("\n") + "\n" if lines else ""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def parse_delta(text: str) -> list:
    """
    解析 DELTA: 块，提取所有操作行。
    操作格式：
      surface: <内容>          → ADD
      hypothesis: <内容>       → ADD
      assessment: <内容>       → ADD (替换旧 assessment 见 apply_delta)
      counter: <内容>          → ADD
      [REMOVE] surface: <前缀> → DELETE
      [UPDATE] surface: <前缀> = <新内容> → UPDATE
    """
    ops = []
    in_delta = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.upper().startswith("DELTA:"):
            in_delta = True
            continue
        if not in_delta:
            continue
        if not stripped:
            continue

        action = "ADD"
        content_line = stripped

        # 检测 [REMOVE] 和 [UPDATE] 指令
        if stripped.upper().startswith("[REMOVE]"):
            action = "DELETE"
            content_line = stripped[len("[REMOVE]"):].strip()
        elif stripped.upper().startswith("[UPDATE]"):
            action = "UPDATE"
            content_line = stripped[len("[UPDATE]"):].strip()

        ops.append({"action": action, "line": content_line})

    return ops


def add_line(lines: list, new_line: str):
    """ADD：追加到 memory 末尾。非 prefix: content 格式的行拒绝写入。"""
    if not _is_valid_line(new_line):
        preview = new_line.strip()[:60]
        print(f"[merge] REJECTED (invalid prefix): {preview}", file=sys.stderr)
        return lines
    lines.append(new_line.rstrip("\n") + "\n")
    return lines


def delete_line(lines: list, prefix: str):
    """DELETE：删除所有以 prefix 开头的行。"""
    return [l for l in lines if not l.strip().startswith(prefix)]


def update_line(lines: list, prefix: str, new_content: str):
    """UPDATE：替换第一条以 prefix 开头的行为 new_content。新内容也必须符合 prefix: content 格式。"""
    if not _is_valid_line(new_content):
        preview = new_content.strip()[:60]
        print(f"[merge] REJECTED UPDATE (invalid prefix): {preview}", file=sys.stderr)
        return lines
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = new_content.rstrip("\n") + "\n"
            return lines
    # 没找到匹配行 → 追加
    lines.append(new_content.rstrip("\n") + "\n")
    return lines


def apply_delta(lines: list, ops: list) -> list:
    """将解析后的操作应用到 memory 行列表。"""
    for op in ops:
        action = op["action"]
        line_text = op["line"]

        if action == "ADD":
            lines = add_line(lines, line_text)
        elif action == "DELETE":
            lines = delete_line(lines, line_text)
        elif action == "UPDATE":
            # UPDATE 格式: "surface: /api/login = 新内容" 或 "surface: /api/login → 新内容"
            parts = re.split(r'\s*[=→]\s*', line_text, maxsplit=1)
            if len(parts) == 2:
                lines = update_line(lines, parts[0].strip(), parts[1].strip())
            else:
                # 无法解析 → 追加为 ADD
                lines = add_line(lines, line_text)

    return lines


def extract_active_target(lines: list) -> str:
    """Active memory 只应有一条 target:；取第一条非空 body。"""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("target:"):
            return stripped[len("target:"):].strip()
    return ""


def target_identity(body: str) -> str:
    """机械身份：target body 第一个空白分隔 token，小写。不判断根域/公司。"""
    if not body:
        return ""
    token = body.split()[0].strip().lower()
    # 去掉常见 URL 壳，只比 host-ish 段
    for prefix in ("https://", "http://"):
        if token.startswith(prefix):
            token = token[len(prefix):]
    token = token.split("/")[0]
    return token


def target_safe_name(body: str) -> str:
    identity = target_identity(body) or "unknown"
    return (
        identity.replace(":", "_")
        .replace("/", "_")
        .replace("\\", "_")
    )


def archive_active_memory(lines: list, old_body: str) -> str:
    """将当前热 memory 落到 results/<safe>/memory.md。返回归档路径。"""
    safe = target_safe_name(old_body)
    dest_dir = os.path.join(RESULTS_DIR, safe)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "memory.md")
    text = "".join(lines).rstrip("\n") + "\n"
    # 若已有归档更长则保留更长（防覆盖更完整世界）；否则写当前
    if os.path.exists(dest):
        try:
            with open(dest, "r", encoding="utf-8") as f:
                existing = f.read()
            if len(existing) > len(text):
                print(
                    f"[merge] archive keep longer existing: {safe} ({len(existing)}>{len(text)})",
                    file=sys.stderr,
                )
                return dest
        except OSError:
            pass
    with open(dest, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[merge] archived active world → {dest}", file=sys.stderr)
    return dest


def reset_turn_state_on_target_change() -> None:
    """换 target：机械清零 Turn event telemetry（.turn_state.json）。"""
    payload = {"version": 1, "turns_without_tool_use": 0}
    try:
        with open(TURN_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError:
        pass


def reset_observe_on_target_change() -> None:
    """换 target 时清零 target-loop 停滞计数 + turn telemetry（机械；不解释业务）。"""
    reset_turn_state_on_target_change()
    if not os.path.exists(OBSERVE_STATE_PATH):
        return
    try:
        with open(OBSERVE_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(state, dict):
        return
    state["consecutive_none"] = 0
    state["same_action_count"] = 0
    state["actions"] = []
    state["memory_offset"] = 0
    if "prefix_counts" in state and isinstance(state["prefix_counts"], dict):
        state["prefix_counts"] = {
            "vuln": 0,
            "hypothesis": 0,
            "surface": 0,
            "cred": 0,
            "total_lines": 0,
            "total_chars": 0,
        }
    try:
        with open(OBSERVE_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        print("[merge] observe counters reset on target change", file=sys.stderr)
    except OSError as err:
        print(f"[merge] observe reset failed: {type(err).__name__}", file=sys.stderr)


def split_ops_at_first_target(ops: list):
    """在第一个 ADD target: 处切开：before / target_op / after。"""
    for i, op in enumerate(ops):
        if op["action"] != "ADD":
            continue
        line = op["line"].strip()
        if line.startswith("target:") and line[len("target:"):].strip():
            before = ops[:i]
            after = ops[i + 1:]
            # 丢掉 after 中后续 target: ADD，避免再叠
            after = [
                o for o in after
                if not (o["action"] == "ADD" and o["line"].strip().startswith("target:"))
            ]
            return before, op, after
    return ops, None, []


def apply_target_boundary(lines: list, ops: list) -> tuple:
    """
    机械 world boundary：
    1) 先把第一个 target: 之前的 ops 应用到当前 world
    2) 若 target 身份变化：archive → clear → init(new)
    3) 再把 target: 之后的 ops 应用到新 world
    返回 (lines, did_transition)。
    """
    before, target_op, after = split_ops_at_first_target(ops)
    if target_op is None:
        return apply_delta(lines, ops), False

    # 旧 world 先收 before（同批里写在新 target 之前的事实仍属旧站）
    lines = apply_delta(lines, before)

    new_line = target_op["line"].strip()
    new_body = new_line[len("target:"):].strip()
    new_id = target_identity(new_body)
    old_body = extract_active_target(lines)
    old_id = target_identity(old_body)

    if not old_id:
        print(f"[merge] init active target: {new_id}", file=sys.stderr)
        lines = [new_line + "\n"]
        lines = apply_delta(lines, after)
        return lines, True

    if new_id == old_id:
        print(f"[merge] same active target ({new_id}); drop duplicate target: line", file=sys.stderr)
        lines = apply_delta(lines, after)
        return lines, False

    print(
        f"[merge] TARGET BOUNDARY: {old_id} → {new_id} (archive+clear+init)",
        file=sys.stderr,
    )
    archive_active_memory(lines, old_body)
    reset_observe_on_target_change()
    lines = [new_line + "\n"]
    lines = apply_delta(lines, after)
    return lines, True


def main():
    # 优先用 reconfigure 后的 text stream；如果不可用则从 buffer 手动解码
    try:
        text = sys.stdin.read()
    except UnicodeDecodeError:
        raw = sys.stdin.buffer.read()
        text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        sys.exit(0)

    ops = parse_delta(text)
    if not ops:
        sys.exit(0)

    lines = read_memory()
    lines, transitioned = apply_target_boundary(lines, ops)
    write_memory(lines)

    added = sum(1 for o in ops if o["action"] == "ADD")
    updated = sum(1 for o in ops if o["action"] == "UPDATE")
    deleted = sum(1 for o in ops if o["action"] == "DELETE")
    boundary = " BOUNDARY:1" if transitioned else ""
    print(f"[merge] ADD:{added} UPDATE:{updated} DELETE:{deleted}{boundary}", file=sys.stderr)


if __name__ == "__main__":
    main()
