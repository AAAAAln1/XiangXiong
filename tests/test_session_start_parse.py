"""projection state visibility 回归测试。

回归点：hypothesis 的 state 可见性不得丢失。

原守卫（v0.3）：全部 state 在单一 HYPOTHESES 段中逐字可见。
现守卫（2026-09-16）：投影拆为 [FRONTIER]/[SETTLED] 两段，但
**分区必须完整**——两段合起来等于全量 unknowns，一行不丢、state 不丢。
这比"头部计数相等"更强：它同时防住了"过滤合法 state"和"拆分时漏行"。

拆分动机（规格 §1.1 E1/E3）：活前沿不能被已解决行淹没；且每条需可寻址（稳定 ID）。
这是按**已有 state 字段**做的结构划分，不新增分类语义。
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime.digest import SWEEP_STATE, hypothesis_id  # noqa: E402
from runtime.project import (  # noqa: E402
    format_frontier_section,
    format_settled_section,
    format_sweep_section,
)
from runtime.world import parse as world_parse  # noqa: E402


def _render(view):
    return (
        (format_frontier_section(view) or "")
        + (format_sweep_section(view) or "")
        + (format_settled_section(view) or "")
    )


class TestHypothesisStateVisibility(unittest.TestCase):
    def setUp(self):
        self.lines = [
            "hypothesis [observed]: A",
            "hypothesis [blocked]: B",
            "hypothesis [confirmed]: C",
            "hypothesis [contradicted]: D",
            "hypothesis [unknown]: E",
            "hypothesis [sweep]: S",
            "hypothesis: F",
            "future: G",
        ]

    def test_partition_is_complete(self):
        """[FRONTIER] + [SWEEP] + [SETTLED] 必须覆盖全部 unknowns 行，一行不丢。"""
        view = world_parse(self.lines)
        out = _render(view)
        for item in view.unknowns:
            self.assertIn(
                hypothesis_id(item), out,
                f"hypothesis 行在投影中丢失: {item.raw}",
            )

    def test_every_state_visible(self):
        """所有 state 都可见：其余五种在行内标注，sweep 整段独立、由段头承载。"""
        out = _render(world_parse(self.lines))
        for state in ("observed", "blocked", "confirmed", "contradicted", "unknown"):
            self.assertIn(f"[{state}]", out)
        self.assertIn("[SWEEP]", out, "sweep 行单独成段，段头即其可见性（行内不再重复标注）")

    def test_verified_goes_to_settled_and_rest_to_frontier(self):
        """confirmed/contradicted → SETTLED；sweep → SWEEP；其余 → FRONTIER。"""
        view = world_parse(self.lines)
        frontier = format_frontier_section(view)
        sweep = format_sweep_section(view)
        settled = format_settled_section(view)
        self.assertIn("[SETTLED]", settled)
        self.assertIn("[SWEEP]", sweep)
        for item in view.unknowns:
            hid = hypothesis_id(item)
            if item.state in ("confirmed", "contradicted"):
                self.assertIn(hid, settled)
                self.assertNotIn(hid, frontier)
                self.assertNotIn(hid, sweep)
            elif item.state == SWEEP_STATE:
                self.assertIn(hid, sweep, "sweep 行必须进 [SWEEP] 段（待派队列）")
                self.assertNotIn(hid, frontier)
                self.assertNotIn(hid, settled)
            else:
                self.assertIn(hid, frontier)
                self.assertNotIn(hid, settled)
                self.assertNotIn(hid, sweep)

    def test_sweep_is_not_settled(self):
        """sweep 是未闭合的工作项，不能落进定局"""
        view = world_parse(["hypothesis [sweep]: 枚举 20 个端点"])
        self.assertNotIn(SWEEP_STATE, ("confirmed", "contradicted"))
        self.assertIn("[SWEEP]", format_sweep_section(view))
        self.assertEqual(format_settled_section(view), "")

    def test_future_is_projected_into_frontier(self):
        """future: 现在进入 [FRONTIER]（此前不投影，见融合规格 §0.2）。"""
        out = format_frontier_section(world_parse(self.lines))
        self.assertIn("next: G", out)
        self.assertIn("[FRONTIER]", out)

    def test_no_schema_external_judgment_words(self):
        """投影不得出现判决性分类词（只按已有 state 分桶）。"""
        out = _render(world_parse(self.lines))
        for banned in ("STALLED", "CLOSED", "ACTIVE", "DEAD FRAME", "PRIORITY"):
            self.assertNotIn(banned, out)

    def test_id_is_stable_across_state_upgrade_and_evidence(self):
        """稳定 ID：补证据或升级状态时不变（merge 的账本更新按同一归一化匹配）。"""
        base = world_parse(["hypothesis [observed]: admin panel may exist at /admin"])
        upgraded = world_parse(
            ["hypothesis [confirmed]: admin panel may exist at /admin (req_042)"]
        )
        self.assertEqual(
            hypothesis_id(base.unknowns[0]), hypothesis_id(upgraded.unknowns[0])
        )

    def test_id_is_mechanical_derivation(self):
        """ID 必须由 memory 行机械派生（同文本恒同号，异文本异号）。"""
        a = world_parse(["hypothesis [observed]: X is vulnerable"])
        b = world_parse(["hypothesis [observed]: X is vulnerable"])
        c = world_parse(["hypothesis [observed]: Y is vulnerable"])
        self.assertEqual(hypothesis_id(a.unknowns[0]), hypothesis_id(b.unknowns[0]))
        self.assertNotEqual(hypothesis_id(a.unknowns[0]), hypothesis_id(c.unknowns[0]))

    def test_empty_view_emits_nothing(self):
        self.assertEqual(format_frontier_section(world_parse([])), "")
        self.assertEqual(format_settled_section(world_parse([])), "")


if __name__ == "__main__":
    unittest.main()
