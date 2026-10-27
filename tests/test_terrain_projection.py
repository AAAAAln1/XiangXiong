"""judge 侧投影：形状计数 + 无假设解释的观察（融合架构 P5）。

P5 的目标**不是缩小体积**（实测投影仅占 200K 上下文的 2–4%，压缩不是价值所在），
而是让 judge 先看到决策信息：
- `[TERRAIN]`：形状（J1）—— 某 kind 有多少、edge 各状态多少
- `[FRONTIER]` / `[EXCLUDED]`：该做什么 / 别做什么（提到 WORLD MODEL 之前）
- `[UNEXPLAINED]`：有观察、无人问（E5 / J4）—— 新框架的唯一种子

不变量：
1. terrain 小计必须等于该类总数（有未标状态的行要显式列为 `none N`，否则读者对不上）
2. unexplained 只在"edge 有锚点可判"时才有意义；observation 自身无锚点则不判定
3. 段序：TERRAIN < FRONTIER < EXCLUDED < WORLD MODEL < … < ARTIFACTS
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import graph, project  # noqa: E402
from runtime.world import parse as world_parse  # noqa: E402


class TestTerrain(unittest.TestCase):
    LINES = [
        "target: t\n",
        "surface: /api/login 返回 200 (req_001)\n",
        "vuln: 未授权读 (req_002)\n",
        "hypothesis [observed]: /api/login 是否可枚举\n",
        "hypothesis: 无状态的假设 /api/login\n",
        "counter: /api/login 参数无影响 (req_009)\n",
        "dead: /old 不可达\n",
        "future: 拿到凭据后重测\n",
    ]

    def setUp(self):
        self.graph = graph.parse(self.LINES)

    def test_counts_are_correct(self):
        rows = dict(((k, s), n) for k, s, n in graph.terrain(self.graph))
        self.assertEqual(rows[(graph.KIND_OBSERVATION, None)], 1)
        self.assertEqual(rows[(graph.KIND_FINDING, None)], 1)
        self.assertEqual(rows[(graph.KIND_EDGE, "observed")], 1)
        self.assertEqual(rows[(graph.KIND_EDGE, None)], 1, "无状态标记的 hypothesis 也应计入")
        self.assertEqual(rows[(graph.KIND_EXCLUSION, None)], 2)

    def test_order_is_stable(self):
        """同一 kind 可有多行（每种 state 一行），但顺序必须按 KIND_ORDER 非递减。"""
        kinds = [kind for kind, _, _ in graph.terrain(self.graph)]
        order = {k: i for i, k in enumerate(graph.KIND_ORDER)}
        self.assertEqual([order[k] for k in kinds], sorted(order[k] for k in kinds))
        # 同一 kind 的行必须相邻（不得交错）
        self.assertEqual(kinds, sorted(kinds, key=lambda k: order[k]))

    def test_blank_and_target_are_not_counted(self):
        sub = graph.terrain(graph.parse(["target: t\n", "\n", "\n"]))
        self.assertEqual(sub, [])

    def test_render_subtotals_equal_total(self):
        out = project.format_terrain_section(self.graph)
        line = [l for l in out.splitlines() if "edge" in l][0]
        self.assertIn("edge 2", line)
        self.assertIn("none 1", line, "未标状态的行必须显式列出，否则小计对不上总数")
        # 「无假设解释的观察数」已移出 [TERRAIN]：它是 [UNEXPLAINED] 段头的计数。
        # 同一条数字出现两次只会让读者怀疑两处是否一致（投影审计 2026-09-18）。
        self.assertNotIn("unexplained", out)


class TestUnexplained(unittest.TestCase):
    def test_observation_without_any_edge_mention_is_unexplained(self):
        g = graph.parse([
            "hypothesis [observed]: /api/login 是否可枚举\n",
            "surface: /api/login 返回 200\n",
            "surface: /totally/other 没人问过\n",
        ])
        got = graph.unexplained_observations(g)
        self.assertEqual(len(got), 1)
        self.assertIn("/totally/other", got[0].text)

    def test_no_edges_with_anchors_means_cannot_judge(self):
        """没有任何 edge 带锚点 → 无法判定"是否被解释" → 必须返回空，不得全塞进来。"""
        g = graph.parse([
            "hypothesis [observed]: 纯中文的假设，没有标识符\n",
            "surface: /api/login 返回 200\n",
        ])
        self.assertEqual(graph.unexplained_observations(g), ())

    def test_observation_without_anchors_is_not_reported(self):
        """observation 自身无锚点时无法判定，不塞进结果（否则是噪音）。"""
        g = graph.parse([
            "hypothesis [observed]: /api/login 是否可枚举\n",
            "surface: 完全中文描述，没有任何标识符\n",
        ])
        self.assertEqual(graph.unexplained_observations(g), ())

    def test_limit_is_respected(self):
        lines = ["hypothesis [observed]: /api/login 是否可枚举\n"]
        for i in range(5):
            lines.append(f"surface: /other{i}/path 没人问过\n")
        g = graph.parse(lines)
        self.assertEqual(len(graph.unexplained_observations(g, limit=2)), 2)

    def test_render_caps_and_announces_remainder(self):
        lines = ["hypothesis [observed]: /api/login 是否可枚举\n"]
        for i in range(13):
            lines.append(f"surface: /other{i}/path 没人问过\n")
        g = graph.parse(lines)
        nodes = graph.unexplained_observations(g, limit=13)
        self.assertEqual(len(nodes), 13)
        out = project.format_unexplained_section(nodes, limit=10)
        self.assertIn("[UNEXPLAINED] (13)", out)
        self.assertIn("(+3 more)", out)
        # 2026-09-18 改为**只列 ID**（正文见 [WORLD MODEL]，那里每行现在都带 ID）：
        # 段头 + ID 行 = 2 行。原先逐条复制 90 字符正文，是同一批行的重复。
        self.assertEqual(len(out.strip().splitlines()), 2)
        for node in nodes[:10]:
            self.assertIn(node.id, out)

    def test_empty_renders_empty(self):
        self.assertEqual(project.format_unexplained_section(()), "")


class TestExcludedSection(unittest.TestCase):
    def test_renders_counters_only(self):
        view = world_parse(["target: t\n", "surface: s\n", "counter: c\n", "dead: d\n"])
        out = project.format_excluded_section(view)
        self.assertIn("[EXCLUDED] (2)", out)
        self.assertIn("counter: c", out)
        self.assertNotIn("surface: s", out)

    def test_empty_when_no_counters(self):
        view = world_parse(["target: t\n", "surface: s\n"])
        self.assertEqual(project.format_excluded_section(view), "")


class TestRealDataTerrain(unittest.TestCase):
    def test_terrain_matches_node_count(self):
        path = os.path.join(PROJECT_ROOT, ".claude", "memory.md")
        if not os.path.isfile(path):
            self.skipTest("无 active memory")
        with open(path, encoding="utf-8") as handle:
            g = graph.parse(handle.readlines())
        stated = sum(n for _, _, n in graph.terrain(g))
        self.assertEqual(stated, len(g.ids()), "terrain 小计必须等于有 ID 的节点数")


if __name__ == "__main__":
    unittest.main()
