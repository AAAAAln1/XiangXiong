"""工具就绪投影（[TOOLS]）不变量。

## 为什么需要这组测试

非 HTTP 路径的每一次"跑了才知道工具不存在"都要花掉一轮上下文
（execute.py 只在执行后返回 tool_not_found）。把缺失提前变成投影事实能省掉这轮，
代价是往热路径**新增一段文本** —— 它必须守住三条边界，否则会退化成"工具菜单"：

1. 只列**缺失**：已存在的二进制不得出现（出现即被读成推荐）。
2. 无缺失则不出现：环境完整时不占投影预算（[TOOLS] 是判据，不是装饰）。
3. 名字过多必须显式截断并给出全文位置（与预算裁剪同一原则：不静默丢信息）。

另锁两条机械事实：探针报出的缺失必须是本机真实缺失；[TOOLS] 的位置在世界状态之后。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import project, toolcheck  # noqa: E402
from runtime.graph import parse as graph_parse  # noqa: E402
from runtime.world import parse as world_parse  # noqa: E402


class _PatchedProbe:
    """临时替换探针结果：只改它的输入，不改探针实现。"""

    def __init__(self, missing, candidates):
        self._missing = list(missing)
        self._candidates = tuple(candidates)

    def __enter__(self):
        self._saved = (toolcheck.missing_binaries, toolcheck.CANDIDATE_BINARIES)
        toolcheck.missing_binaries = lambda *a, **k: list(self._missing)
        toolcheck.CANDIDATE_BINARIES = self._candidates
        return self

    def __exit__(self, *exc):
        toolcheck.missing_binaries, toolcheck.CANDIDATE_BINARIES = self._saved
        return False


def _listed_names(text: str) -> list:
    """从 [TOOLS] 正文行解析出被列出的工具名。

    2026-09-18 格式变更：原先是 "missing N/M: a, b, c (+k more) | full: …"（两行），
    现在压成单行 "缺 N/M — 这些手段在本机不可用（别把它们算进计划）：a/b/c… | 详见 …"。
    """
    body = text.split("不可用（别把它们算进计划）：", 1)[1]
    body = body.split(" | ")[0].rstrip("…")
    return [name for name in body.split("/") if name]


class TestProbeFacts(unittest.TestCase):
    def test_reported_missing_are_really_missing(self):
        for name in toolcheck.missing_binaries():
            self.assertIsNone(shutil.which(name), f"{name} 本机存在却被报为缺失")

    def test_partition_covers_candidates_without_overlap(self):
        present = set(toolcheck.present_binaries())
        missing = set(toolcheck.missing_binaries())
        self.assertFalse(present & missing, "同一二进制不能既 present 又 missing")
        self.assertEqual(present | missing, set(toolcheck.CANDIDATE_BINARIES))

    def test_runtime_required_is_inside_candidates(self):
        """硬依赖必须也在候选集内，否则投影里看不出主路径已断。"""
        for name in toolcheck.RUNTIME_REQUIRED:
            self.assertIn(name, toolcheck.CANDIDATE_BINARIES)


class TestToolsSection(unittest.TestCase):
    def test_absent_when_nothing_missing(self):
        with _PatchedProbe((), ("curl",)):
            self.assertEqual(project.format_tools_section(), "")

    def test_lists_only_missing(self):
        with _PatchedProbe(("absent-a", "absent-b"), ("absent-a", "absent-b")):
            text = project.format_tools_section()
        self.assertEqual(_listed_names(text), ["absent-a", "absent-b"])
        self.assertIn("缺 2/2", text)

    def test_real_projection_lists_only_missing(self):
        text = project.format_tools_section()
        if not text:
            self.skipTest("本机候选集全部存在，本用例无从验证")
        for name in _listed_names(text):
            self.assertIsNone(shutil.which(name), f"{name} 本机存在却被投影为缺失")

    def test_cap_is_explicit_never_silent(self):
        names = [f"absent-tool-{i:02d}" for i in range(25)]
        with _PatchedProbe(names, tuple(names) + ("filler",) * 5):
            text = project.format_tools_section()
        listed = _listed_names(text)
        self.assertEqual(len(listed), project.TOOLS_NAMES_IN_PROJECTION)
        self.assertEqual(listed[0], "absent-tool-00")
        self.assertNotIn(f"absent-tool-{project.TOOLS_NAMES_IN_PROJECTION:02d}", listed)
        self.assertIn("…", text, "截断必须是可见的省略号，不能静默丢掉")
        self.assertIn("runtime/toolcheck.py", text, "截断后必须给出全文位置")

    def test_position_after_world_state(self):
        lines = ["target: tools.example.test\n"]
        with _PatchedProbe(("absent-tool-00",), ("absent-tool-00",)):
            sections = project._compose_sections(world_parse(lines), graph_parse(lines))
        names = [name for name, _ in sections]
        self.assertIn("TOOLS", names)
        self.assertLess(names.index("WORLD MODEL"), names.index("TOOLS"))


if __name__ == "__main__":
    unittest.main()
