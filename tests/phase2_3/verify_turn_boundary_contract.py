#!/usr/bin/env python3
"""
Turn Event Telemetry — 契约冻结验证脚本（非 idle detection）。

验证三件事：
1) .turn_state.json 不写入 memory.md / WORLD 投影
2) decision:block 仅作对话合同债务；turn/stagnation/repetition 不触发新自动化控制
3) 模拟多轮 Stop：turn 输出仅两数字字段；无 idle/stuck 等禁词；
   turn JSON 与 stagnation JSON 分离（后者可并存但不渗进 turn 对象）

用法（在 xiangxiong 根目录）：
  python tests/phase2_3/verify_turn_boundary_contract.py
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
STOP_PATH = ROOT / "scripts" / "stop_check.py"
RUNTIME = ROOT / "runtime"

# turn 输出禁止出现的「Agent 状态 / 控制」词（小写匹配）
FORBIDDEN_IN_TURN_PAYLOAD = (
    "idle",
    "stuck",
    "health",
    "productivity",
    "agent_state",
    "confidence",
    "recommend",
    "should_",
    "next_action",
    "choose",
    "router",
    "planner",
    "watch",  # action_idle_level 类
)

# turn 对象键白名单
TURN_KEYS_ALLOWED = frozenset({"turn_tool_use_count", "turns_without_tool_use"})


class Failures(list):
    def check(self, cond: bool, msg: str) -> None:
        if cond:
            print(f"  PASS  {msg}")
        else:
            print(f"  FAIL  {msg}")
            self.append(msg)


def load_stop_check(turn_path: str, observe_path: str):
    spec = importlib.util.spec_from_file_location("stop_check_verify", STOP_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    mod.TURN_STATE_PATH = turn_path
    mod.STATE_PATH = observe_path
    return mod


def write_jsonl(path: Path, rows: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def user_text(t: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": t}}


def assistant_text(t: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": t}],
        },
    }


def assistant_with_tools(n: int) -> dict:
    blocks: list = [{"type": "text", "text": "run"}]
    for i in range(n):
        blocks.append(
            {
                "type": "tool_use",
                "id": f"tool_{i}",
                "name": "Bash",
                "input": {"command": f"echo {i}"},
            }
        )
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def run_stop(mod, transcript: Path) -> tuple[list[dict], str]:
    stdin = json.dumps({"transcript_path": str(transcript)})
    buf = io.StringIO()
    with mock.patch.object(mod.sys, "stdin", io.StringIO(stdin)):
        with redirect_stdout(buf):
            try:
                mod.main()
            except SystemExit as e:
                if e.code not in (0, None):
                    raise
    raw = buf.getvalue()
    parsed = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed.append(json.loads(line))
    return parsed, raw


def section(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def part1_turn_not_in_world(f: Failures) -> None:
    section("1) turn_state 不写入 world model / memory.md")

    sys.path.insert(0, str(RUNTIME))
    import merge as merge_mod  # type: ignore
    import project as project_mod  # type: ignore
    import world as world_mod  # type: ignore

    with tempfile.TemporaryDirectory(prefix="xx_turn_world_") as tmp:
        tmp_p = Path(tmp)
        mem = tmp_p / "memory.md"
        turn_p = tmp_p / ".turn_state.json"
        observe_p = tmp_p / ".observe_state.json"

        # 污染 turn_state：若投影读它，会暴露
        turn_p.write_text(
            json.dumps({"version": 1, "turns_without_tool_use": 99}, ensure_ascii=False),
            encoding="utf-8",
        )
        observe_p.write_text(
            json.dumps(
                {
                    "version": 3,
                    "consecutive_none": 0,
                    "last_round": 0,
                    "actions": [],
                    "prefix_counts": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        mem.write_text("target: boundary.test\nsurface: only-this\n", encoding="utf-8")

        # --- merge DELTA 不得把 turn 键写进 memory ---
        old_active = os.environ.get("XIANGXIONG_WORLD_ACTIVE")
        os.environ["XIANGXIONG_WORLD_ACTIVE"] = str(mem)
        merge_mod.OBSERVE_STATE_PATH = str(observe_p)
        merge_mod.TURN_STATE_PATH = str(turn_p)
        try:
            # 直接 stdin 风格：调用 parse + apply 路径
            text = (
                "DELTA:\n"
                "surface: after-delta\n"
                "turns_without_tool_use: 99\n"  # 非法前缀，应 REJECT
                "turn_tool_use_count: 0\n"
            )
            ops = merge_mod.parse_delta(text)
            lines = merge_mod.read_memory()
            # apply via public apply_delta if exists
            if hasattr(merge_mod, "apply_delta"):
                new_lines = merge_mod.apply_delta(lines, ops)
            else:
                new_lines = lines
                for op in ops:
                    if op["action"] == "ADD":
                        new_lines = merge_mod.add_line(new_lines, op["line"])
            merge_mod.write_memory(new_lines)
            body = mem.read_text(encoding="utf-8")
            f.check("turns_without_tool_use" not in body, "merge 拒绝后 memory 无 turns_without_tool_use")
            f.check("turn_tool_use_count" not in body, "merge 拒绝后 memory 无 turn_tool_use_count")
            f.check("surface: after-delta" in body or "after-delta" in body, "合法 surface 仍可写入")
            f.check("99" not in body or "surface" in body, "memory 不因 turn_state 文件出现 99 作为 turn 字段")
        finally:
            if old_active is None:
                os.environ.pop("XIANGXIONG_WORLD_ACTIVE", None)
            else:
                os.environ["XIANGXIONG_WORLD_ACTIVE"] = old_active

        # --- world.parse 无 turn 字段 ---
        view = world_mod.parse(mem.read_text(encoding="utf-8").splitlines())
        raw_view = repr(view)
        f.check("turns_without_tool_use" not in raw_view, "world.parse 视图不含 turns_without_tool_use")
        f.check("turn_tool_use_count" not in raw_view, "world.parse 视图不含 turn_tool_use_count")

        # --- compile() 投影：不读 turn_state（用 monkeypatch 路径）---
        # project 使用 project_root() 下的 .observe_state 与 world_active
        # 将 cwd/隔离：写临时 project 根不现实；改为静态源码 + 行为断言：
        # 1) project 源码不引用 turn_state
        # 2) 在隔离 env 下 compile 输出不含 turn 键名
        proj_src = (RUNTIME / "project.py").read_text(encoding="utf-8")
        f.check(".turn_state" not in proj_src, "project.py 源码不引用 .turn_state")
        f.check("turns_without_tool_use" not in proj_src, "project.py 源码无 turns_without_tool_use")
        f.check("turn_tool_use_count" not in proj_src, "project.py 源码无 turn_tool_use_count")

        asm_src = (ROOT / "context" / "assembler.py").read_text(encoding="utf-8")
        f.check("turn_state" not in asm_src and "turns_without_tool_use" not in asm_src,
                "assembler.py 不引用 turn telemetry")

        # 行为：world_active + 伪造 observe，compile 输出
        os.environ["XIANGXIONG_WORLD_ACTIVE"] = str(mem)
        # project._load_observe_state 读 project_root()/.observe_state.json
        # 将 ROOT 下临时不碰真实文件：patch project_mod 内部
        real_obs = project_mod._load_observe_state

        def fake_obs():
            return {
                "consecutive_none": 2,
                "last_round": 3,
                "actions": [{"fp": "HTTP:GET:x", "round": 1}],
            }

        project_mod._load_observe_state = fake_obs  # type: ignore
        try:
            # _read_memory_lines 走 world_active
            out = project_mod.compile()
        finally:
            project_mod._load_observe_state = real_obs  # type: ignore
            os.environ.pop("XIANGXIONG_WORLD_ACTIVE", None)

        f.check("turns_without_tool_use" not in out, "compile() 输出无 turns_without_tool_use")
        f.check("turn_tool_use_count" not in out, "compile() 输出无 turn_tool_use_count")
        f.check("[WORLD MODEL]" in out or "target:" in out or "boundary.test" in out,
                "compile() 仍含 world 内容")
        f.check("[OBSERVE]" in out, "compile() 仍可有 [OBSERVE]（来自 observe_state）")
        # turn 文件存在且为 99，但输出不应出现该 streak 作为 turn 字段
        f.check(
            "turns_without_tool_use: 99" not in out and '"turns_without_tool_use": 99' not in out,
            "compile 不泄露 turn_state 磁盘值 99",
        )


def part2_block_debt_only(f: Failures) -> None:
    section("2) decision:block 仅债务路径；telemetry 不触发新控制")

    with tempfile.TemporaryDirectory(prefix="xx_block_") as tmp:
        turn_p = os.path.join(tmp, ".turn_state.json")
        obs_p = os.path.join(tmp, ".observe_state.json")
        # 高 stagnation 原料：若错误接线可能「自动控制」
        with open(obs_p, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "version": 3,
                    "consecutive_none": 20,
                    "actions": [{"fp": "HTTP:GET:same", "round": i} for i in range(12)],
                    "prefix_counts": {"vuln": 0},
                },
                fh,
            )
        sc = load_stop_check(turn_p, obs_p)
        tr = Path(tmp) / "t.jsonl"

        # 2a 纯无 tool：有 turn 输出，无 decision
        write_jsonl(tr, [user_text("go"), assistant_text("只思考不调用工具")])
        parsed, raw = run_stop(sc, tr)
        f.check(all(p.get("decision") != "block" for p in parsed), "无 tool 轮不产生 decision:block")
        f.check(any("turn_tool_use_count" in p for p in parsed), "无 tool 轮有 turn telemetry")
        turn_objs = [p for p in parsed if "turn_tool_use_count" in p]
        f.check(all(set(p.keys()) <= TURN_KEYS_ALLOWED for p in turn_objs), "turn 对象键 ⊆ 白名单")

        # 2b 高 stagnation：可有 stagnation_level，但仍无因 telemetry 的 block
        # 上一轮已 K>=1；再跑一轮无 tool
        write_jsonl(tr, [user_text("go2"), assistant_text("继续纯文本")])
        parsed2, raw2 = run_stop(sc, tr)
        f.check(all(p.get("decision") != "block" for p in parsed2), "高 stagnation + 无 tool 仍不 block")
        has_stag = any("stagnation_level" in p for p in parsed2)
        has_turn = any("turn_tool_use_count" in p for p in parsed2)
        f.check(has_turn, "无 tool 仍输出 turn")
        # stagnation 可能 warning+（consecutive_none=20）
        f.check(has_stag, "高 consecutive_none 可输出 stagnation（观测，非控制）")
        for p in parsed2:
            if "turn_tool_use_count" in p:
                f.check("stagnation_level" not in p, "turn JSON 不混入 stagnation_level")
                f.check("decision" not in p, "turn JSON 无 decision")

        # 2c 源码：可执行 decision:block 仅 1 处且在 conflict 执法段；main 里 (2a) 之后无 block
        src = STOP_PATH.read_text(encoding="utf-8")
        f.check("update_turn_telemetry" in src, "存在 update_turn_telemetry")

        def strip_py_comments(text: str) -> str:
            out_lines = []
            for ln in text.splitlines():
                s = ln.strip()
                if s.startswith("#"):
                    continue
                if "#" in ln:
                    ln = ln[: ln.index("#")]
                out_lines.append(ln)
            return "\n".join(out_lines)

        exec_src = strip_py_comments(src)
        block_assigns = list(
            re.finditer(r'["\']decision["\']\s*:\s*["\']block["\']', exec_src)
        )
        f.check(len(block_assigns) == 1, "可执行 decision:block 字面仅 1 处（债务执法）")
        if block_assigns:
            pos = block_assigns[0].start()
            window = exec_src[max(0, pos - 400) : pos]
            f.check(
                "conflict_reason" in window,
                "唯一 decision:block 位于 conflict_reason 执法分支",
            )
        # main 内 call 之后（用 (2a) 标记，避免命中函数 def 行）
        idx_2a = src.find("# --- (2a) turn event telemetry")
        f.check(idx_2a > 0, "存在 (2a) turn telemetry 段标记")
        after_2a = strip_py_comments(src[idx_2a:])
        f.check(
            not re.search(r'["\']decision["\']\s*:\s*["\']block["\']', after_2a),
            "(2a) turn 段及之后无新的可执行 decision:block",
        )

        # 2d 真正 block：仅对话合同；且不更新 turn（债务隔离）
        sc.save_turn_state(7)
        write_jsonl(tr, [user_text("q"), assistant_text("需要我继续吗？")])
        parsed3, raw3 = run_stop(sc, tr)
        f.check(len(parsed3) == 1 and parsed3[0].get("decision") == "block", "「是否继续」→ 唯一 block")
        f.check("turn_tool_use_count" not in raw3, "block 时不输出 turn（先 exit）")
        f.check(sc.load_turn_state()["turns_without_tool_use"] == 7, "block 不推进 turns_without_tool_use")

        # 2e 有 tool：无 turn 输出、无 block
        write_jsonl(
            tr,
            [user_text("u"), assistant_with_tools(2), assistant_text("done")],
        )
        parsed4, raw4 = run_stop(sc, tr)
        f.check(not any("turn_tool_use_count" in p for p in parsed4), "有 tool 轮静默 turn")
        f.check(all(p.get("decision") != "block" for p in parsed4), "有 tool 正常轮不 block")
        f.check(sc.load_turn_state()["turns_without_tool_use"] == 0, "有 tool 后 K 清零")


def part3_session_sim_telemetry_log(f: Failures) -> None:
    section("3) 完整会话模拟：turn 日志无 idle/stuck 等禁词")

    with tempfile.TemporaryDirectory(prefix="xx_sim_") as tmp:
        turn_p = os.path.join(tmp, ".turn_state.json")
        obs_p = os.path.join(tmp, ".observe_state.json")
        with open(obs_p, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "version": 3,
                    "consecutive_none": 0,
                    "actions": [],
                    "prefix_counts": {},
                },
                fh,
            )
        sc = load_stop_check(turn_p, obs_p)
        tr = Path(tmp) / "session.jsonl"
        log_lines: list[str] = []

        def step(label: str, rows: list) -> list:
            write_jsonl(tr, rows)
            parsed, raw = run_stop(sc, tr)
            log_lines.append(f"--- {label} ---")
            log_lines.append(raw if raw.strip() else "(no stdout)")
            for p in parsed:
                log_lines.append(f"PARSED: {json.dumps(p, ensure_ascii=False)}")
            return parsed

        # 模拟：用户目标 → 三轮无 tool → 一轮有 tool → 再一轮无 tool
        step("T1_no_tool", [user_text("测 example.com"), assistant_text("先分析结构")])
        step("T2_no_tool", [user_text("继续"), assistant_text("再想想")])
        step(
            "T3_no_tool",
            [user_text("继续"), assistant_text("仍无 tool")],
        )
        p3 = step(
            "T3b_check",
            [user_text("?"), assistant_text("第三轮无 tool 结束")],
        )
        # 上面每 step 都是独立 transcript 文件重写=新 human turn；K 按每次 Stop +1
        # 重新设计为「追加式」会话文件更真实
        log_lines.clear()
        sc.save_turn_state(0)
        session_rows: list = []

        def append_and_stop(label: str, new_rows: list) -> list:
            session_rows.extend(new_rows)
            write_jsonl(tr, session_rows)
            parsed, raw = run_stop(sc, tr)
            log_lines.append(f"=== {label} ===")
            log_lines.append(raw.strip() or "(silent)")
            return parsed

        p = append_and_stop("turn1_no_tool", [user_text("目标 A"), assistant_text("纯文本1")])
        f.check(
            len(p) == 1 and p[0] == {"turn_tool_use_count": 0, "turns_without_tool_use": 1},
            f"turn1 输出精确两字段 K=1 实际={p}",
        )

        p = append_and_stop("turn2_no_tool", [user_text("继续"), assistant_text("纯文本2")])
        f.check(
            p and p[0].get("turns_without_tool_use") == 2,
            f"turn2 K=2 实际={p}",
        )

        p = append_and_stop(
            "turn3_with_tools",
            [user_text("开干"), assistant_with_tools(3)],
        )
        f.check(p == [] or not any("turn_tool_use_count" in x for x in p), "turn3 有 tool → turn 静默")

        p = append_and_stop("turn4_no_tool_after", [user_text("小结"), assistant_text("又纯文本")])
        f.check(
            p and p[0].get("turns_without_tool_use") == 1 and p[0].get("turn_tool_use_count") == 0,
            f"tool 后无 tool 轮 K 从 1 再计 实际={p}",
        )

        full_log = "\n".join(log_lines)
        print()
        print("----- SESSION TELEMETRY LOG -----")
        print(full_log)
        print("----- END LOG -----")

        low = full_log.lower()
        for bad in FORBIDDEN_IN_TURN_PAYLOAD:
            # stagnation_level 允许出现在「同时存在的第二行 JSON」，但不允许在 turn 对象内
            if bad == "watch":
                f.check(bad not in low, f"会话日志无禁词 {bad!r}")
            else:
                f.check(bad not in low, f"会话日志无禁词 {bad!r}")

        # turn 行解析：每一行若含 turn_tool_use_count，不得含禁键/stagnation
        for line in full_log.splitlines():
            if "turn_tool_use_count" not in line:
                continue
            if line.startswith("PARSED:"):
                obj = json.loads(line[len("PARSED:") :].strip())
            elif line.strip().startswith("{"):
                obj = json.loads(line.strip())
            else:
                continue
            extra = set(obj.keys()) - TURN_KEYS_ALLOWED
            f.check(not extra, f"turn 对象无额外键 extra={extra}")
            blob = json.dumps(obj).lower()
            for bad in FORBIDDEN_IN_TURN_PAYLOAD:
                f.check(bad not in blob, f"turn 对象 JSON 无 {bad}")
            f.check("stagnation_level" not in obj, "turn 对象不含 stagnation_level")
            f.check("decision" not in obj, "turn 对象不含 decision")

        # 源码级：stop_check turn_payload 构造不含禁词键
        src = STOP_PATH.read_text(encoding="utf-8")
        m = re.search(
            r"turn_payload\s*=\s*\{([^}]+)\}",
            src,
            re.DOTALL,
        )
        f.check(bool(m), "源码存在 turn_payload 字面量")
        if m:
            body = m.group(1)
            f.check("turn_tool_use_count" in body and "turns_without_tool_use" in body,
                    "turn_payload 仅合同两字段名")
            f.check("idle" not in body.lower() and "stuck" not in body.lower(),
                    "turn_payload 字面无 idle/stuck")


def main() -> int:
    print("XiangXiong Turn Boundary Contract Verification")
    print(f"ROOT={ROOT}")
    fails = Failures()
    try:
        part1_turn_not_in_world(fails)
        part2_block_debt_only(fails)
        part3_session_sim_telemetry_log(fails)
    except Exception:
        traceback.print_exc()
        fails.append(f"EXCEPTION: {traceback.format_exc()[-500:]}")

    section("SUMMARY")
    if fails:
        print(f"FAILED {len(fails)} checks:")
        for m in fails:
            print(f"  - {m}")
        return 1
    print("ALL CHECKS PASSED")
    print("Contract: turn_state ⟂ WORLD/memory; block=debt only; turn log keys closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
