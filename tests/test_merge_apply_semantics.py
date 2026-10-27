"""merge 应用语义：计数必须反映**真实效果**，失败必须**显式**。

## 背景（实测踩到的两个坑，会话 71b3f780）

1. **`DELETE:n` 说谎。** 旧实现 `sum(1 for o in ops if o["action"] == "DELETE")`
   数的是 ops 条数。于是：
   ```
   [REMOVE] hypothesis: X        → [merge] DELETE:1
   ```
   但 `X` 在账本里实际是 `hypothesis [observed]: X` —— 前缀逐字符不匹配，
   `delete_line` 静默返回原列表，**一行都没删**。LLM 据此以为收口完成。
   它为此多花了 3 轮（06:23→06:24）才自己翻源码看出真相。

2. **`delete_line` 缺对称的失败提示。** `update_line` 早有 F.5-② 的
   "UPDATE FAILED + 去哪里看正确前缀"，`delete_line` 没有。

## 锁死的不变量

1. `stats` 统计**真正生效**的操作数；被拒/未匹配/重复 → 计入 `NOOP`，不计入对应动作
2. `DELETE` 未匹配 → `DELETE FAILED` 提示，且提示里要说清**状态标记**这个坑
3. `ADD` 被证据闸门拒绝 → 不计入 `ADD`
4. `stats["added"]` 只在生效时收集原文（供 sweep 队列提示用）
5. target boundary 两侧的 stats 必须累积，不能只报一半
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import merge  # noqa: E402

MEMORY = [
    "target: merge.example.test\n",
    "surface: /login 存在 (req_001)\n",
    "hypothesis [observed]: /api/user 是否可枚举 (req_002)\n",
    "hypothesis [sweep]: 枚举 20 个端点 (req_003)\n",
]


def _run_delta(text: str, lines=None) -> tuple:
    """跑一次 DELTA，返回 (lines, stats, stderr)。"""
    ops = merge.parse_delta(text)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result, stats = merge.apply_delta(list(lines if lines is not None else MEMORY), ops)
    return result, stats, buf.getvalue()


class TestDeleteCountTellsTheTruth(unittest.TestCase):
    def test_delete_with_matching_prefix_counts_one(self):
        _, stats, err = _run_delta("DELTA:\n[REMOVE] surface: /login 存在\n")
        self.assertEqual(stats["DELETE"], 1)
        self.assertEqual(stats["NOOP"], 0)
        self.assertNotIn("DELETE FAILED", err)

    def test_delete_without_match_counts_zero_and_says_so(self):
        """核心回归：这一行以前会报 DELETE:1，但什么都没删。"""
        result, stats, err = _run_delta("DELTA:\n[REMOVE] surface: /nonexistent\n")
        self.assertEqual(stats["DELETE"], 0, "未匹配不得计入 DELETE")
        self.assertEqual(stats["NOOP"], 1)
        self.assertIn("DELETE FAILED", err)
        self.assertEqual(result, MEMORY, "行数不得变化")

    def test_status_bracket_is_the_actual_trap(self):
        """`hypothesis: X` 匹配不到 `hypothesis [observed]: X` —— 提示必须点破这件事。"""
        result, stats, err = _run_delta(
            "DELTA:\n[REMOVE] hypothesis: /api/user 是否可枚举 (req_002)\n"
        )
        self.assertEqual(stats["DELETE"], 0)
        self.assertIn("DELETE FAILED", err)
        self.assertIn("hypothesis [observed]", err, "提示必须给出正确的带状态前缀写法")
        self.assertEqual(len(result), len(MEMORY))

    def test_exact_prefix_with_status_deletes(self):
        result, stats, err = _run_delta(
            "DELTA:\n[REMOVE] hypothesis [observed]: /api/user 是否可枚举\n"
        )
        self.assertEqual(stats["DELETE"], 1)
        self.assertLess(len(result), len(MEMORY))

    def test_rejected_delete_is_not_counted(self):
        """删 confirmed/vuln 被拒 → 不算 DELETE，且必须报出来。"""
        lines = MEMORY + ["hypothesis [confirmed]: x 已证 (req_009)\n"]
        _, stats, err = _run_delta(
            "DELTA:\n[REMOVE] hypothesis [confirmed]: x 已证\n", lines=lines
        )
        self.assertEqual(stats["DELETE"], 0)
        self.assertIn("REJECTED DELETE", err)


class TestAddAndUpdateCountsAreHonest(unittest.TestCase):
    def test_add_counts_only_when_it_lands(self):
        _, stats, _ = _run_delta("DELTA:\nsurface: /new 存在 (req_010)\n")
        self.assertEqual(stats["ADD"], 1)
        self.assertEqual(stats["added"], ["surface: /new 存在 (req_010)"])

    def test_add_rejected_by_evidence_gate_is_noop(self):
        """confirmed 无 exp- 证据 → 闸门拒绝 → 不得计入 ADD。"""
        _, stats, err = _run_delta("DELTA:\nhypothesis [confirmed]: x 已证\n")
        self.assertEqual(stats["ADD"], 0, "被闸门拒绝的行没进账本，不能算 ADD")
        self.assertEqual(stats["NOOP"], 1)
        self.assertIn("REJECTED", err)

    def test_duplicate_surface_is_appended_not_deduped(self):
        """非 hypothesis 行**不去重**：ADD 一条已存在的 surface 会真的追加一行。

        这是既有语义（`add_line` 只对 hypothesis 走 ledger update）。
        这里锁住它，是因为"静默去重"会让 ADD 计数重新开始说谎 ——
        账本说写了、内存没变，与本次要修的病同源。
        """
        result, stats, _ = _run_delta("DELTA:\nsurface: /login 存在 (req_001)\n")
        self.assertEqual(stats["ADD"], 1)
        self.assertEqual(len(result), len(MEMORY) + 1)

    def test_duplicate_hypothesis_does_not_grow_memory(self):
        """同归一化文本的 hypothesis 行走就地更新，账本不增长。"""
        result, stats, _ = _run_delta(
            "DELTA:\nhypothesis [observed]: /api/user 是否可枚举 (req_002)\n"
        )
        self.assertEqual(len(result), len(MEMORY), "同文本 hypothesis 不该新增行")
        self.assertEqual(stats["ADD"], 0, "没有新增就不能计入 ADD")

    def test_update_miss_is_not_counted(self):
        _, stats, err = _run_delta(
            "DELTA:\n[UPDATE] surface: /nope = surface: /nope 改了 (req_011)\n"
        )
        self.assertEqual(stats["UPDATE"], 0)
        self.assertEqual(stats["NOOP"], 1)
        self.assertIn("UPDATE FAILED", err)

    def test_update_hit_is_counted(self):
        _, stats, _ = _run_delta(
            "DELTA:\n[UPDATE] surface: /login 存在 = surface: /login 是 Vue SPA (req_001)\n"
        )
        self.assertEqual(stats["UPDATE"], 1)

    def test_stats_always_expose_the_same_keys(self):
        _, stats, _ = _run_delta("DELTA:\nsurface: /x 存在 (req_012)\n")
        for key in ("ADD", "UPDATE", "DELETE", "NOOP", "added"):
            self.assertIn(key, stats)


class TestBoundaryStatsAccumulate(unittest.TestCase):
    """ops 被 target boundary 拆成两批时，计数必须两批都算，不能只报一半。"""

    def test_stats_cover_both_sides_of_boundary(self):
        text = (
            "DELTA:\n"
            "surface: /old-side 存在 (req_020)\n"
            "target: other.example.test\n"
            "surface: /new-side 存在 (req_021)\n"
        )
        ops = merge.parse_delta(text)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            lines, stats, transitioned = merge.apply_target_boundary(list(MEMORY), ops)
        self.assertTrue(transitioned)
        self.assertEqual(stats["ADD"], 2, "boundary 两侧的 ADD 都要计入")
        self.assertEqual(lines[0].strip(), "target: other.example.test")


class TestSweepQueueHint(unittest.TestCase):
    """写 `[sweep]` 行后必须提示去看待派队列 —— 否则队列只是账本里的一行字。

    2026-09-18：原先这里提示一次就结束（`warn_sweep_queue`）。现在回吐整个决策摘要
    （`merge.emit_decision_digest`），`[sweep]` 只是它覆盖的一种情形 ——
    它同时覆盖新增/闭合普通 hypothesis、改 goal、换 target。
    逐项语义见 `tests/test_merge_digest.py`，这里只保留"`[sweep]` 行会触发"这一条。
    """

    def _emit(self, lines):
        buf = io.StringIO()
        before = merge.decision_zone([])
        with contextlib.redirect_stderr(buf):
            merge.emit_decision_digest(lines, before)
        return buf.getvalue()

    def test_warns_when_a_sweep_line_lands(self):
        err = self._emit([
            "target: t.example.com\n",
            "hypothesis [sweep]: 枚举 20 个端点\n",
        ])
        self.assertIn("[NEXT]", err)
        self.assertIn("project.py", err, "要给出看全文的指针")
        self.assertIn('Agent(subagent_type="deepdive"', err)

    def test_digest_replaces_the_one_off_sweep_hint(self):
        """旧的局部提示已删除 —— 两套提示会给出两个版本的队列。"""
        self.assertFalse(hasattr(merge, "warn_sweep_queue"))


class TestArchiveSubcommand(unittest.TestCase):
    """归档原先只在 target 边界触发，agent 只能自己写脚本 —— 现在有 CLI。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xx_archive_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.memory = os.path.join(self.tmp, "memory.md")
        with open(self.memory, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(MEMORY))
        self.results = os.path.join(self.tmp, "results")
        os.makedirs(self.results, exist_ok=True)

    def _run(self, argv):
        out = io.StringIO()
        with mock.patch.object(merge, "world_active", lambda: self.memory), \
                mock.patch.object(merge, "RESULTS_DIR", self.results), \
                contextlib.redirect_stdout(out):
            merge.cmd_archive(argv)
        return out.getvalue()

    def test_archives_active_world(self):
        out = self._run(["archive"])
        dest = os.path.join(self.results, merge.target_safe_name("merge.example.test"), "memory.md")
        self.assertTrue(os.path.isfile(dest))
        self.assertIn(dest, out)
        with open(dest, encoding="utf-8") as handle:
            self.assertIn("hypothesis [sweep]", handle.read())

    def test_rejects_mismatched_target(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(["archive", "some.other.target"])
        self.assertEqual(ctx.exception.code, 1)

    def test_accepts_matching_target(self):
        self._run(["archive", "merge.example.test"])

    def test_fails_loudly_without_target_line(self):
        empty = os.path.join(self.tmp, "empty.md")
        with open(empty, "w", encoding="utf-8") as handle:
            handle.write("surface: 没有 target 行\n")
        with mock.patch.object(merge, "world_active", lambda: empty), \
                mock.patch.object(merge, "RESULTS_DIR", self.results):
            with self.assertRaises(SystemExit) as ctx:
                merge.cmd_archive(["archive"])
        self.assertEqual(ctx.exception.code, 1)

    def test_keeps_longer_existing_archive(self):
        """已有归档更长 → 保留它（防覆盖更完整的世界），但仍返回路径。"""
        dest_dir = os.path.join(self.results, merge.target_safe_name("merge.example.test"))
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "memory.md")
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write("x" * 100_000)
        out = self._run(["archive"])
        self.assertIn(dest, out)
        with open(dest, encoding="utf-8") as handle:
            self.assertEqual(len(handle.read()), 100_000)


class TestCliSubcommandDispatch(unittest.TestCase):
    def test_unknown_subcommand_exits_2(self):
        import subprocess
        proc = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "runtime", "merge.py"), "nope"],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("未知子命令", proc.stderr)


class TestEmptyNeighborhoodIsAnnounced(unittest.TestCase):
    """空切片必须显式说出来：分不清"没邻居"和"切片坏了"，反应会完全相反。"""

    def test_render_marks_empty_slice(self):
        from runtime import graph
        g = graph.parse([
            "hypothesis [sweep]: 刚写下的新工作，memory 里还没有邻居 (req_001)\n",
        ])
        node = g.by_kind(graph.KIND_EDGE)[0]
        text = graph.render_neighborhood(graph.neighborhood(g, node.id))
        self.assertIn("empty slice", text)
        self.assertIn("不要改读全量 memory", text)

    def test_no_marker_when_slice_has_neighbours(self):
        from runtime import graph
        g = graph.parse([
            "hypothesis [observed]: /api/user/list 是否可枚举 (req_001)\n",
            "surface: /api/user/list 返回 200 (req_002)\n",
        ])
        node = [n for n in g.by_kind(graph.KIND_EDGE) if n.id][0]
        text = graph.render_neighborhood(graph.neighborhood(g, node.id))
        self.assertNotIn("empty slice", text)

    def test_cli_reports_empty_slice_flag(self):
        """`deepdive.py` 的 JSON 必须带 `empty_slice`，子 agent 一眼可判。"""
        import subprocess
        from runtime import graph as gmod

        # 用绝不会撞名的 target；它会在真实 results/ 下落一个目录，跑完清掉。
        target = "xx-empty-slice-probe.example.test"
        created = os.path.join(PROJECT_ROOT, "results", merge.target_safe_name(target))
        self.addCleanup(shutil.rmtree, created, True)
        os.makedirs(created, exist_ok=True)
        world = os.path.join(created, "memory.md")
        world_lines = [f"target: {target}\n", "hypothesis [sweep]: 没有邻居的新工作 (req_001)\n"]
        with open(world, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(world_lines))

        intent = [n for n in gmod.parse(world_lines).by_kind(gmod.KIND_EDGE) if n.id][0].id
        proc = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "runtime", "deepdive.py"),
             target, "--intent", intent],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env=dict(os.environ, PYTHONUTF8="1"),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["empty_slice"], "这条 intent 没有共享锚点的邻居")
        self.assertEqual(payload["related"], 0)


if __name__ == "__main__":
    unittest.main()
