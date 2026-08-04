#!/usr/bin/env python3
"""Turn event telemetry — Stop 独占；非 idle detection。"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
STOP_PATH = ROOT / "scripts" / "stop_check.py"


def _load_stop_check():
    spec = importlib.util.spec_from_file_location("stop_check_under_test", STOP_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _write_jsonl(path: Path, rows: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _user_text(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _assistant_tools(n: int, preface: str = "go") -> dict:
    blocks = [{"type": "text", "text": preface}]
    for i in range(n):
        blocks.append(
            {
                "type": "tool_use",
                "id": f"t{i}",
                "name": "Bash",
                "input": {"command": f"echo {i}"},
            }
        )
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def _tool_result_user() -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t0", "content": "ok"}],
        },
    }


class TestTurnTelemetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xx_turn_")
        self.sc = _load_stop_check()
        self.sc.TURN_STATE_PATH = os.path.join(self.tmp, ".turn_state.json")
        self.sc.STATE_PATH = os.path.join(self.tmp, ".observe_state.json")
        self.transcript = Path(self.tmp) / "t.jsonl"

    def _run_main(self, rows: list) -> tuple:
        _write_jsonl(self.transcript, rows)
        stdin = json.dumps({"transcript_path": str(self.transcript)})
        from io import StringIO

        out = StringIO()
        with mock.patch.object(self.sc.sys, "stdin", StringIO(stdin)):
            with mock.patch.object(self.sc.sys, "stdout", out):
                try:
                    self.sc.main()
                except SystemExit as exc:
                    self.assertEqual(exc.code, 0)
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        parsed = [json.loads(ln) for ln in lines]
        return parsed, out.getvalue()

    def test_zero_tool_turn_emits_two_fields(self):
        parsed, raw = self._run_main(
            [_user_text("测站"), _assistant_text("先想一下不做 tool")]
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(
            parsed[0],
            {"turn_tool_use_count": 0, "turns_without_tool_use": 1},
        )
        self.assertNotIn("decision", parsed[0])
        self.assertNotIn("idle", raw)
        self.assertNotIn("watch", raw)
        self.assertNotIn("stuck", raw)
        self.assertNotIn("stagnation_level", raw)

    def test_consecutive_no_tool_increments(self):
        self._run_main([_user_text("u1"), _assistant_text("a1")])
        parsed, _ = self._run_main([_user_text("u2"), _assistant_text("a2")])
        self.assertEqual(parsed[0]["turns_without_tool_use"], 2)
        parsed, _ = self._run_main([_user_text("u3"), _assistant_text("a3")])
        self.assertEqual(parsed[0]["turn_tool_use_count"], 0)
        self.assertEqual(parsed[0]["turns_without_tool_use"], 3)

    def test_tool_use_resets_and_silent(self):
        self._run_main([_user_text("u1"), _assistant_text("no tool")])
        self.assertEqual(
            self.sc.load_turn_state()["turns_without_tool_use"], 1
        )
        parsed, raw = self._run_main(
            [
                _user_text("u2"),
                _assistant_tools(2),
                _tool_result_user(),
                _assistant_text("done with tools earlier in turn"),
            ]
        )
        # 本轮有 tool_use → 不打印 turn 字段
        self.assertEqual(parsed, [])
        self.assertEqual(raw.strip(), "")
        self.assertEqual(
            self.sc.load_turn_state()["turns_without_tool_use"], 0
        )

    def test_tool_result_user_does_not_reset_turn_boundary(self):
        """tool_result 行不是 human boundary；计数累计自上一 human user。"""
        rows = [
            _user_text("go"),
            _assistant_tools(1),
            _tool_result_user(),
            _assistant_text("follow-up text same turn"),
        ]
        n = self.sc.count_turn_tool_uses
        _write_jsonl(self.transcript, rows)
        self.assertEqual(n(str(self.transcript)), 1)

    def test_no_tool_never_block(self):
        parsed, raw = self._run_main(
            [_user_text("x"), _assistant_text("纯文本无继续询问无DELTA")]
        )
        self.assertTrue(parsed)
        self.assertNotIn("decision", raw)
        self.assertNotIn("block", raw)

    def test_block_path_unchanged_and_skips_telemetry_side_effects_on_conflict(self):
        """合同 enforcement 仍可 block；本测只断言 block 文案与 decision。"""
        # 含「是否继续」→ block；注意：main 在 block 时 exit，不更新 turn
        # 先 seed turn state
        self.sc.save_turn_state(5)
        parsed, raw = self._run_main(
            [
                _user_text("q"),
                _assistant_text("需要我继续吗？"),
            ]
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].get("decision"), "block")
        self.assertIn("reason", parsed[0])
        # block 在 telemetry 之前：turns 应仍为 5（未 +1）
        self.assertEqual(self.sc.load_turn_state()["turns_without_tool_use"], 5)
        self.assertNotIn("turn_tool_use_count", raw)

    def test_compute_level_unchanged(self):
        self.assertEqual(self.sc.compute_level(0, 0), "normal")
        self.assertEqual(self.sc.compute_level(5, 0), "warning")
        self.assertEqual(self.sc.compute_level(9, 0), "critical")
        self.assertEqual(self.sc.compute_level(15, 10), "emergency")

    def test_stagnation_independent_of_turn(self):
        """consecutive_none 高时仍出 stagnation；turn 另起一行；none 语义不读 turn。"""
        observe = {
            "version": 3,
            "consecutive_none": 6,
            "actions": [{"fp": "HTTP:GET:x", "round": 1}] * 2,
            "prefix_counts": {"vuln": 0},
        }
        with open(self.sc.STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(observe, f)
        parsed, raw = self._run_main(
            [_user_text("u"), _assistant_text("no tool again")]
        )
        kinds = {tuple(sorted(p.keys())) for p in parsed}
        self.assertIn(("turn_tool_use_count", "turns_without_tool_use"), kinds)
        stag = [p for p in parsed if "stagnation_level" in p]
        self.assertEqual(len(stag), 1)
        self.assertEqual(stag[0]["stagnation_level"], "warning")
        self.assertEqual(stag[0]["metrics"]["consecutive_none_rounds"], 6)
        self.assertNotIn("turns_without_tool_use", stag[0])
        self.assertNotIn("idle", raw)

    def test_merge_target_switch_clears_turn_state(self):
        sys.path.insert(0, str(ROOT / "runtime"))
        import merge as merge_mod  # type: ignore

        merge_mod.TURN_STATE_PATH = self.sc.TURN_STATE_PATH
        merge_mod.OBSERVE_STATE_PATH = self.sc.STATE_PATH
        self.sc.save_turn_state(9)
        merge_mod.reset_observe_on_target_change()
        with open(self.sc.TURN_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("turns_without_tool_use"), 0)


if __name__ == "__main__":
    unittest.main()
