"""projection state visibility 回归测试。

回归点：hypothesis 从 observed 转移到 confirmed/contradicted 后，
必须仍在 projection（format_unknown_sections）中可见。
防止未来再次发生 state visibility 丢失（schema drift：过滤合法 state）。
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime.project import _hypothesis_display_text, format_unknown_sections  # noqa: E402
from runtime.world import parse as world_parse  # noqa: E402


class TestHypothesisStateVisibility(unittest.TestCase):
    def setUp(self):
        self.lines = [
            "hypothesis [observed]: A",
            "hypothesis [blocked]: B",
            "hypothesis [confirmed]: C",
            "hypothesis [contradicted]: D",
        ]

    def test_all_states_visible_in_projection(self):
        """全量 state 必须出现在 projection：confirmed/contradicted 不被过滤。"""
        view = world_parse(self.lines)
        out = format_unknown_sections(view)
        self.assertIn("hypothesis: A  [observed]", out)
        self.assertIn("hypothesis: B  [blocked]", out)
        self.assertIn("hypothesis: C  [confirmed]", out)
        self.assertIn("hypothesis: D  [contradicted]", out)

    def test_no_schema_external_grouping_words(self):
        """投影不得出现 schema 外分类词（OPEN/STALLED/CLOSED/ACTIVE）。"""
        view = world_parse(self.lines)
        out = format_unknown_sections(view)
        for banned in ("OPEN UNKNOWNS", "STALLED", "CLOSED", "ACTIVE"):
            self.assertNotIn(banned, out)

    def test_hypotheses_bucket_mirrors_unknowns_count(self):
        """bucket 计数必须等于 schema unknowns 行数（含 confirmed/contradicted）。"""
        view = world_parse(self.lines)
        out = format_unknown_sections(view)
        self.assertIn(f"HYPOTHESES ({len(view.unknowns)}):", out)

    def test_projection_lines_traceable_to_memory(self):
        """投影每一行必须可回指 memory 原始行（逐字镜像，不解释）。"""
        view = world_parse(self.lines)
        out = format_unknown_sections(view)
        # 投影行 = _hypothesis_display_text(raw) + 可选 [state] 后缀，全部由 memory 行机械派生
        for item in view.unknowns:
            disp = _hypothesis_display_text(item)
            if item.state:
                self.assertIn(f"  {disp}  [{item.state}]", out)
            else:
                self.assertIn(f"  {disp}", out)


if __name__ == "__main__":
    unittest.main()
