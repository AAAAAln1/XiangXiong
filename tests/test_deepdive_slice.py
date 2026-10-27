"""邻域切片器（B：派发前的机械准备）。

## 背景

派发从"自建嵌套 claude 子进程"改成宿主 Task 工具后，`deepdive.py` 的职责收窄成
**只剪邻域**：由 intent-id 从图取该前沿项，落一个**有界切片**文件，把路径交给探索者。

## 为什么切片文件存在

Cairn 的做法是 prompt 只带图的路径（图多大都不占 prompt）。这里是同一个意思：
探索人的 prompt 里只有一个路径字符串，它的世界视图全部来自那个文件 ——
所以那个文件必须是**有界的**（`RENDER_LIMIT`），否则"隔离上下文"就是假的。

## 锁死的不变量

1. 切片文件必须真的落盘、可读、含 intent 本身与边界声明（outside）
2. 切片 ≠ 全量 memory：行数必须显著小于源文件
3. intent 不存在 → `(None, None)` + 可执行的失败提示（不静默、不猜 id）
4. 该模块不得有任何派发副作用（那是 B 的回归守卫，见 test_neighborhood.py）
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import deepdive  # noqa: E402

TARGET = "dispatch.example.test"

MEMORY = """\
target: dispatch.example.test
surface: /api/user/list 返回 200 (req_001)
surface: /api/user/detail 返回 200 (req_002)
hypothesis [observed]: /api/user/list 是否可未授权枚举
hypothesis [sweep]: /api/user/list 的 229 个兄弟接口逐个探测未授权
counter: /api/user/list 加参数无影响 (req_009)
dead: /api/user/old 不可达
surface: /totally/unrelated/path 与上面无共享锚点 (req_010)
surface: /another/isolated/path 也没有共享锚点 (req_011)
surface: /third/isolated/path 同样没有 (req_012)
"""


class DeepdiveSliceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xx_slice_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        target_dir = os.path.join(self.tmp, deepdive.safe_target(TARGET))
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, "memory.md"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(MEMORY)
        self.patch = mock.patch.object(deepdive, "RESULTS_DIR", self.tmp)
        self.patch.start()
        self.addCleanup(self.patch.stop)

        # 取一个真实存在的 intent id
        from runtime.graph import KIND_EDGE, parse as graph_parse
        graph = graph_parse(MEMORY.splitlines(keepends=True))
        self.intent_id = graph.by_kind(KIND_EDGE)[0].id


class TestWriteNeighborhood(DeepdiveSliceCase):
    def test_creates_a_readable_file(self):
        path, found = deepdive.write_neighborhood(TARGET, self.intent_id)
        self.assertIsNotNone(path, "切片应落盘")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.path.basename(path), self.intent_id.replace(":", "-") + ".txt")

    def test_slice_states_intent_anchors_and_outside(self):
        path, found = deepdive.write_neighborhood(TARGET, self.intent_id)
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn(f"[NEIGHBORHOOD {self.intent_id}]", body)
        self.assertIn("anchors:", body)
        self.assertIn("outside:", body)
        self.assertIn("excluded (", body)

    def test_slice_includes_same_anchor_exclusions(self):
        """同锚点的 counter/dead 必须进切片 —— 那是"这一族已死"，最省探索时间的信息。"""
        path, _ = deepdive.write_neighborhood(TARGET, self.intent_id)
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("/api/user/list 加参数无影响", body)

    def test_slice_is_bounded_not_full_memory(self):
        """切片必须显著小于源：等于全长就说明"隔离上下文"是假的。"""
        path, _ = deepdive.write_neighborhood(TARGET, self.intent_id)
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        source_lines = len([ln for ln in MEMORY.splitlines() if ln.strip()])
        slice_lines = len([ln for ln in body.splitlines() if ln.strip()])
        self.assertLessEqual(slice_lines, deepdive.RENDER_LIMIT + 8, "切片超出渲染上限")
        self.assertLess(slice_lines, source_lines, "切片不得退化成全量 memory")

    def test_unknown_intent_returns_none(self):
        path, found = deepdive.write_neighborhood(TARGET, "h:000000")
        self.assertIsNone(path)
        self.assertIsNone(found)

    def test_unknown_target_returns_none(self):
        path, found = deepdive.write_neighborhood("no-such-target.example", self.intent_id)
        self.assertIsNone(path)
        self.assertIsNone(found)

    def test_rerun_is_idempotent(self):
        first, _ = deepdive.write_neighborhood(TARGET, self.intent_id)
        with open(first, encoding="utf-8") as handle:
            before = handle.read()
        second, _ = deepdive.write_neighborhood(TARGET, self.intent_id)
        with open(second, encoding="utf-8") as handle:
            after = handle.read()
        self.assertEqual(first, second)
        self.assertEqual(before, after)


class TestMemoryPathResolution(DeepdiveSliceCase):
    def test_falls_back_to_archived_memory_when_target_differs(self):
        path = deepdive.memory_path_for(TARGET)
        self.assertTrue(path.endswith(os.path.join(deepdive.safe_target(TARGET), "memory.md")))

    def test_active_memory_is_preferred_when_target_matches(self):
        """活跃 memory 的 target 与请求一致时必须用活跃的（那是最新世界）。"""
        active = os.path.join(self.tmp, "active.md")
        with open(active, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"target: {TARGET}\nsurface: x\n")
        with mock.patch.object(deepdive, "world_active", lambda: active):
            self.assertEqual(deepdive.memory_path_for(TARGET), active)

    def test_active_memory_ignored_when_target_differs(self):
        active = os.path.join(self.tmp, "active.md")
        with open(active, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("target: some.other.target\nsurface: x\n")
        with mock.patch.object(deepdive, "world_active", lambda: active):
            self.assertNotEqual(deepdive.memory_path_for(TARGET), active)


class TestCliContract(unittest.TestCase):
    def test_module_is_importable_and_exposes_slice_api(self):
        for name in ("write_neighborhood", "memory_path_for", "safe_target", "fail_actionable"):
            self.assertTrue(hasattr(deepdive, name), f"缺少 {name}")

    def test_missing_intent_fails_loudly_with_actionable_hint(self):
        """失败必须可执行：给出原因 + 去哪儿找正确的 id，而不是裸 traceback。"""
        proc = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "runtime", "deepdive.py"),
             "no-such-target.example", "--intent", "h:000000"],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn('"ok": false', proc.stdout)
        self.assertIn("intent 不存在", proc.stdout)
        self.assertIn("hint", proc.stdout)
        self.assertIn("[FRONTIER]", proc.stdout, "提示必须说清 id 从哪来")

    def test_help_documents_that_it_does_not_dispatch(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "runtime", "deepdive.py"), "--help"],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("不派发", proc.stdout)


if __name__ == "__main__":
    unittest.main()
